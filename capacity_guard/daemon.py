"""Detached local watcher; no dependence on a running model request."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import codex_home, load_settings, state_dir
from .log_source import LogSource, SourceUnavailable
from .recovery import RecoveryEngine
from .state import Store


def is_running(directory: Path | None = None) -> bool:
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "daemon.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def start(directory: Path | None = None) -> None:
    directory = directory or state_dir()
    if is_running(directory):
        return
    package_root = Path(__file__).resolve().parents[1]
    script = package_root / "scripts" / "capacity_guard.py"
    command = [sys.executable, str(script), "run"] if script.exists() else [sys.executable, "-m", "capacity_guard", "run"]
    environment = os.environ.copy()
    environment["CODEX_CAPACITY_GUARD_HOME"] = str(directory)
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=environment, cwd=package_root)


def run(directory: Path | None = None) -> int:
    from .backends import CodexBackend

    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "daemon.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        os.chmod(directory / "daemon.lock", 0o600)
        store = Store(directory)
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        backend = None
        try:
            settings = load_settings(directory)
            backend = CodexBackend(settings)
            engine = RecoveryEngine(store, backend, settings)
            source = LogSource(codex_home() / "logs_2.sqlite")
            store.set("pid", os.getpid())
            # A crash between sending and recording acceptance is ambiguous.
            store.db.execute("UPDATE recoveries SET status='uncertain' WHERE status='sending'")
            while not stop.is_set() and store.get("enabled", False):
                store.set("heartbeat", time.time())
                try:
                    cursor = store.get("cursor")
                    identity = list((source.path.stat().st_dev, source.path.stat().st_ino))
                    previous_identity = store.get("log_identity")
                    if cursor is None or previous_identity != identity or source.high_watermark() < cursor:
                        # Rebuilt logs and first use start at the current high-water
                        # mark; never replay old errors from another installation.
                        cursor = source.high_watermark()
                        store.set("cursor", cursor)
                        store.set("log_identity", identity)
                    for _ in range(20):
                        events, next_cursor = source.read_after(cursor, limit=2000)
                        for event in events:
                            if event.timestamp >= store.get("enabled_at", 0):
                                engine.observe(event)
                        store.set("cursor", next_cursor)
                        if next_cursor == cursor:
                            break
                        cursor = next_cursor
                    store.set("last_issue", None)
                    engine.tick()
                except (SourceUnavailable, OSError) as error:
                    store.set("last_issue", type(error).__name__)
                except Exception as error:
                    # Keep running through transient local outages, without ever
                    # persisting a raw exception containing conversation content.
                    store.set("last_issue", type(error).__name__)
                stop.wait(settings.poll_interval)
        finally:
            if backend:
                backend.close()
            store.set("pid", None)
            store.close()
        return 0

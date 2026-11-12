"""Optional isolated smoke for the native CLI queue backend.

Run with a Codex binary that supports the local app-server control socket:
  python3 tests/smoke_queue.py /path/to/codex

Only a temporary home, synthetic conversations and a loopback model are used.
The capacity log is a synthetic SQLite fixture. The failed-turn history, queue
submission and completed recovery use the actual Codex server and CLI.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capacity_guard.backends import CodexBackend
from capacity_guard.config import Settings
from capacity_guard.log_source import LogSource
from capacity_guard.recovery import RecoveryEngine
from capacity_guard.state import Store
from capacity_guard.transport import AppServerClient
from smoke_transport import ModelStub, stop_model_stub, stop_process, wait_turn


@contextmanager
def isolated_server(codex_bin: str):
    ModelStub.requests_seen = []
    ModelStub.fail = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="cg-queue-", dir="/tmp") as temporary:
            root = Path(temporary)
            workspace, isolated, home = root / "workspace", root / "codex", root / "home"
            for path in (workspace, isolated, home):
                path.mkdir()
            (isolated / "config.toml").write_text(f'''
model = "capacity-smoke"
model_provider = "capacity_smoke"
approval_policy = "never"
sandbox_mode = "read-only"
check_for_update_on_startup = false
[analytics]
enabled = false
[feedback]
enabled = false
[model_providers.capacity_smoke]
name = "Offline capacity smoke"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 2000
''')
            environment = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT", "LANG") if key in os.environ}
            environment.update(HOME=str(home), CODEX_HOME=str(isolated), XDG_CONFIG_HOME=str(home / ".config"))
            with patch.dict(os.environ, environment, clear=True):
                process = subprocess.Popen(
                    [codex_bin, "app-server", "--listen", "unix://"],
                    cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                owner = None
                try:
                    socket = isolated / "app-server-control" / "app-server-control.sock"
                    deadline = time.monotonic() + 10
                    while not socket.exists():
                        if process.poll() is not None:
                            raise AssertionError("Isolated app-server exited before listening")
                        if time.monotonic() >= deadline:
                            raise AssertionError("Isolated app-server did not create its control socket")
                        time.sleep(.05)
                    owner = AppServerClient(codex_bin, str(socket.resolve()), timeout=10)
                    yield root, workspace, isolated, owner, environment
                finally:
                    if owner:
                        owner.close()
                    stop_process(process)
                    stop_model_stub(server, server_thread)
    finally:
        stop_model_stub(server, server_thread)


def smoke(codex_bin: str, queue_bin: str | None = None) -> dict:
    with isolated_server(codex_bin) as (root, workspace, _, owner, _):
        thread_id = owner.request("thread/start", {"cwd": str(workspace), "historyMode": "legacy"})["thread"]["id"]
        first = owner.start_turn(thread_id, "Synthetic offline capacity test.")
        first_id = first["turn"]["id"]
        failed = wait_turn(owner, thread_id, first_id)
        assert failed["status"] == "failed", "First synthetic turn must fail"

        # The native HTTP stub produces a failed persisted turn, but its 503
        # wrapper is not the runtime's exact capacity error. Feed a separate,
        # synthetic new-format log into the real reader; never edit Codex's DB.
        log_path = root / "synthetic_logs.sqlite"
        body = (
            f'thread{{thread.id="{thread_id}"}}:turn{{turn.id="{first_id}" model="capacity-smoke"}}'
            f':session_task.run:run_turn{{conversation.id="{thread_id}" turn.id="{first_id}"}}: '
            'Turn error: Selected model is at capacity. Please try a different model.'
        )
        with sqlite3.connect(log_path) as writer:
            writer.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, target TEXT, feedback_log_body TEXT, thread_id TEXT)")
            writer.execute("INSERT INTO logs VALUES(1,?,0,?,?,?)", (int(time.time()), "codex_core::session::turn", body, thread_id))
        writer.close()
        events, _ = LogSource(log_path).read_after(0)
        assert len(events) == 1 and events[0].capacity, "Synthetic capacity event must be detected"
        event = events[0]
        assert event.model == "capacity-smoke", "Detected model must match the original turn"

        settings = Settings(codex_bin=queue_bin or codex_bin, backend="queue", initial_delay=1, max_delay=1, poll_interval=.2, jitter=0)
        backend = CodexBackend(settings)
        store = Store(root / "guard")
        try:
            store.set("enabled", True)
            now = [time.time()]
            engine = RecoveryEngine(store, backend, settings, lambda: now[0], lambda: .5)
            engine.observe(event)
            engine.tick()
            assert store.recovery(thread_id)["attempts"] == 0, "Recovery must wait for its deadline"
            ModelStub.fail = False
            now[0] = store.recovery(thread_id)["next_at"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                engine.tick()
                state = store.recovery(thread_id)
                if state["status"] == "recovered":
                    break
                assert state["status"] not in {"cancelled", "uncertain"}, f"Recovery stopped: {state['status']} ({state['reason']})"
                now[0] += .2
                time.sleep(.05)
            assert state["status"] == "recovered", f"Native queue recovery did not complete: {state['status']} ({state['reason']})"
            assert state["attempts"] == 1, "One capacity failure must result in one continuation"
            latest = owner.read_thread(thread_id)["turns"][-1]
            assert latest["id"] != first_id and latest["status"] == "completed"
            assert set(ModelStub.requests_seen) == {"capacity-smoke"}, "Model must stay unchanged"
            assert len(ModelStub.requests_seen) == 2, "Only the failed turn and one recovery may reach the provider"
            return {
                "failed_turn": failed["status"], "retry_turn": latest["status"],
                "runtime_log_synthetic": True, "guard_status": state["status"],
                "queue_attempts": state["attempts"], "same_thread": True, "same_model": True,
                "local_provider_requests": len(ModelStub.requests_seen),
            }
        finally:
            backend.close()
            store.close()


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit("Usage: python3 tests/smoke_queue.py /path/to/server_codex [queue_codex]")
    print(json.dumps(smoke(*sys.argv[1:]), ensure_ascii=False))

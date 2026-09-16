"""Command-line setup, diagnostics and hook entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from . import __version__
from .config import Settings, codex_home, find_codex, load_settings, save_settings, state_dir
from .daemon import is_running, run, start
from .log_source import LogSource, SourceUnavailable
from .recovery import MESSAGE_PREFIX
from .state import Store


def thread_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("Thread must be a UUID.") from None


def hook(event: str) -> int:
    """Hooks only synchronize cancellation and restart an enabled watcher."""
    try:
        payload = json.loads(sys.stdin.read(1024 * 1024))
        thread_id = thread_uuid(payload.get("session_id", ""))
        store = Store(state_dir())
        try:
            with store.transaction():
                if event == "UserPromptSubmit":
                    text = payload.get("prompt", "")
                    # Only recognize our exact outstanding token, not a general
                    # prefix a user might quote in conversation.
                    pending = store.recovery(thread_id)
                    expected = f"{MESSAGE_PREFIX}{pending['token']}]" if pending and pending.get("token") else None
                    own = expected is not None and isinstance(text, str) and text.startswith(expected + "\n") and pending["status"] in {"sending", "uncertain", "submitted"}
                    # Returning the TUI to its input prompt can emit this hook
                    # after a capacity failure. Keep a still-waiting recovery;
                    # the watcher validates the latest persisted turn itself.
                    if pending and pending["status"] in {"sending", "uncertain", "submitted"} and not own:
                        store.cancel(thread_id, "user_prompt")
                elif event in {"Interrupt", "SessionEnd"}:
                    store.cancel(thread_id, "interrupt" if event == "Interrupt" else "session_end")
            enabled = store.get("enabled", False)
        finally:
            store.close()
        if enabled and event != "SessionEnd":
            start()
    except (ValueError, OSError, argparse.ArgumentTypeError):
        pass
    print("{}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for the original Codex model and continue capacity-failed conversations.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    enable = commands.add_parser("enable", help="Enable recovery for new capacity errors and start the watcher")
    enable.add_argument("--thread", type=thread_uuid, action="append", default=[])
    commands.add_parser("disable", help="Disable recovery and cancel pending retries")
    cancel = commands.add_parser("cancel", help="Cancel the pending recovery for one conversation")
    cancel.add_argument("--thread", type=thread_uuid, required=True)
    status = commands.add_parser("status", help="Show local recovery status")
    status.add_argument("--json", action="store_true")
    doctor = commands.add_parser("doctor", help="Read-only compatibility checks")
    doctor.add_argument("--thread", type=thread_uuid)
    commands.add_parser("run", help="Run the watcher in the foreground")
    configure = commands.add_parser("configure", help="Configure waiting intervals and local connection")
    configure.add_argument("--initial-delay", type=float)
    configure.add_argument("--max-delay", type=float)
    configure.add_argument("--poll-interval", type=float)
    configure.add_argument("--jitter", type=float)
    configure.add_argument("--codex-bin")
    configure.add_argument("--socket-path")
    configure.add_argument("--backend", choices=["auto", "queue", "desktop", "app-server"])
    hook_parser = commands.add_parser("hook", help=argparse.SUPPRESS)
    hook_parser.add_argument("event", choices=["SessionStart", "UserPromptSubmit", "Interrupt", "SessionEnd"])
    args = parser.parse_args(argv)
    if args.command == "hook":
        return hook(args.event)
    if args.command == "run":
        return run()
    try:
        if args.command == "configure":
            values = asdict(load_settings())
            for name in values:
                value = getattr(args, name, None)
                if value is not None:
                    values[name] = value
            save_settings(Settings(**values))
            print("Configuration saved. Disable and enable to apply it to a running watcher.")
            return 0
        if args.command == "doctor":
            from .backends import CodexBackend
            settings = load_settings()
            source = LogSource(codex_home() / "logs_2.sqlite")
            result = {"version": __version__, "codex_bin": settings.codex_bin or find_codex()}
            try:
                source.high_watermark()
                result["log_source"] = "ready"
            except SourceUnavailable:
                result["log_source"] = "unavailable"
            backend = CodexBackend(settings)
            try:
                result.update(backend.diagnose(args.thread))
            finally:
                backend.close()
            print(json.dumps(result, indent=2))
            return 0 if result.get("log_source") == "ready" and result.get("connection") == "ready" else 1
        store = Store(state_dir())
        try:
            if args.command == "cancel":
                with store.transaction():
                    store.cancel(args.thread, "user_cancelled")
                print("Pending recovery cancelled for " + args.thread)
                return 0
            if args.command == "enable":
                source = LogSource(codex_home() / "logs_2.sqlite")
                with store.transaction():
                    if not store.get("enabled", False):
                        try:
                            store.set("cursor", source.high_watermark())
                            st = source.path.stat()
                            store.set("log_identity", [st.st_dev, st.st_ino])
                        except (SourceUnavailable, OSError):
                            store.set("cursor", None)
                        store.set("enabled_at", time.time())
                    store.set("threads", args.thread)
                    if args.thread:
                        for row in store.db.execute("SELECT thread_id FROM recoveries WHERE status IN ('waiting','sending','uncertain','submitted')").fetchall():
                            if row[0] not in args.thread:
                                store.cancel(row[0], "scope_changed")
                    store.set("enabled", True)
                start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not is_running():
                    time.sleep(.05)
                if not is_running():
                    print("Enabled, but the watcher did not start. Run `doctor`, then `run`.", file=sys.stderr)
                    return 1
                print("Enabled. Waiting for new capacity errors; original model, unlimited retries.")
                return 0
            if args.command == "disable":
                with store.transaction():
                    store.set("enabled", False)
                    store.db.execute("UPDATE recoveries SET status='cancelled',reason='disabled' WHERE status IN ('waiting','sending','uncertain','submitted')")
                    store.audit("disabled")
                print("Disabled. Pending retries cancelled; an already running Codex turn is left running.")
                return 0
            result = store.status()
            result["running"] = is_running()
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"Recovery: {'enabled' if result['enabled'] else 'disabled'}; watcher: {'running' if result['running'] else 'stopped'}")
                if result["last_issue"]:
                    print("Local issue: " + result["last_issue"])
                for item in result["recoveries"]:
                    wait = max(0, int(item["next_at"] - time.time()))
                    print(f"{item['thread_id']}  {item['model']}  {item['status']}  attempts={item['attempts']}  next={wait}s  {item['reason']}")
            return 0
        finally:
            store.close()
    except (OSError, ValueError) as error:
        print(f"Capacity Guard configuration is unavailable ({type(error).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

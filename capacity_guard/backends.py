"""Read the current conversation, then resume through its existing owner."""

from __future__ import annotations

import re
import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

from .config import Settings, codex_home, find_codex
from .recovery import MESSAGE_PREFIX, Snapshot
from .transport import AppServerClient, TransportError


class BackendUnavailable(TransportError):
    outcome_unknown = False


class QueueOutcomeUnknown(TransportError):
    outcome_unknown = True


class QueueUnavailable(BackendUnavailable):
    outcome_unknown = False


def snapshot_from_thread(thread: dict) -> Snapshot:
    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        raise BackendUnavailable("Conversation has no readable latest turn")
    turn = turns[-1]
    turn_id, status = turn.get("id"), turn.get("status")
    if not isinstance(turn_id, str) or not isinstance(status, str):
        raise BackendUnavailable("Unsupported conversation state")
    runtime = thread.get("status")
    if isinstance(runtime, dict) and runtime.get("type") in {"active", "running"}:
        status = "inProgress"
    if thread.get("pendingSubmissions") or thread.get("pendingApprovals"):
        status = "inProgress"
    token = None
    for item in turn.get("items", []):
        if item.get("type") != "userMessage":
            continue
        for content in item.get("content", []):
            text = content.get("text", "")
            if isinstance(text, str) and text.startswith(MESSAGE_PREFIX):
                match = re.match(re.escape(MESSAGE_PREFIX) + r"([a-f0-9-]{36})\]\n", text)
                if match:
                    token = match[1]
    source = thread.get("source")
    child = isinstance(source, dict) and any("subagent" in str(key).lower() for key in source)
    return Snapshot(
        turn_id=turn_id, status=status,
        model=thread.get("model") or "", recovery_token=token,
        eligible=not (child or thread.get("archived") or thread.get("parentThreadId")),
        has_draft=thread.get("hasDraft") is True,
    )


class CodexBackend:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None
        self.kind = ""
        self.expected: dict[str, str] = {}

    def _connect(self):
        if self.client is not None:
            return self.client
        kind = self.settings.backend
        if kind == "auto":
            # Normal CLI sessions are reached through the stable queue command.
            kind = "app-server" if self.settings.socket_path else "queue"
        if kind == "queue":
            self.client = QueueBackend(self.settings)
            self.kind = kind
            return self.client
        if kind == "desktop":
            from .desktop import DesktopClient
            self.client = DesktopClient(self.settings.socket_path, timeout=10)
        else:
            self.client = AppServerClient(self.settings.codex_bin or find_codex(), self.settings.socket_path, timeout=10)
        self.kind = kind
        return self.client

    def inspect(self, thread_id: str) -> Snapshot:
        try:
            client = self._connect()
            if self.kind == "queue":
                result = client.inspect(thread_id)
            else:
                result = snapshot_from_thread(client.read_thread(thread_id))
            self.expected[thread_id] = result.turn_id
            return result
        except Exception:
            self.close()
            raise

    def resume(self, thread_id: str, message: str, model: str) -> str | None:
        client = self._connect()
        expected = self.expected.get(thread_id)
        if not expected:
            raise BackendUnavailable("Inspect the failed turn before continuation")
        if self.kind == "desktop":
            result = client.start_turn(thread_id, message, model, expected_turn_id=expected)
        elif self.kind == "queue":
            current = client.inspect(thread_id)
            if not current.eligible or current.turn_id != expected or current.status != "failed" or current.has_draft or (current.model and current.model != model):
                raise BackendUnavailable("Conversation changed before continuation")
            client.resume(thread_id, message, model)
            return None
        else:
            current = snapshot_from_thread(client.read_thread(thread_id))
            if not current.eligible or current.turn_id != expected or current.status != "failed" or current.has_draft or (current.model and current.model != model):
                raise BackendUnavailable("Conversation changed before continuation")
            # Omit all settings so existing model, effort and permissions persist.
            result = client.start_turn(thread_id, message)
        turn = result.get("turn")
        return turn.get("id") if isinstance(turn, dict) else None

    def diagnose(self, thread_id: str | None = None) -> dict:
        if thread_id:
            try:
                result = self.inspect(thread_id)
                return {"connection": "ready", "backend": self.kind, "latest_turn_status": result.status, "model_known": bool(result.model), "delivery_tested": False}
            except Exception as error:
                return {"connection": "unavailable", "reason": type(error).__name__, "delivery_tested": False}
        # Without a thread this checks only local prerequisites, not app state.
        kind = self.settings.backend
        if kind == "auto":
            kind = "app-server" if self.settings.socket_path else "queue"
        path = Path(self.settings.socket_path) if self.settings.socket_path else codex_home() / "ipc" / "ipc.sock" if kind == "desktop" else None
        available = False
        if path:
            try:
                available = stat.S_ISSOCK(path.stat().st_mode)
            except OSError:
                pass
        return {"connection": "socket_present_unverified" if available else "unavailable", "backend": kind, "delivery_tested": False, "next_step": "Run doctor --thread UUID to check an existing conversation; this does not submit a turn."}

    def close(self) -> None:
        if self.client:
            self.client.close()
        self.client = None


class QueueBackend:
    """Continue an ordinary local Codex CLI session via `codex queue`."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.codex_bin = settings.codex_bin or find_codex()

    def _state_db(self) -> Path:
        candidates = sorted(codex_home().glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates:
            if path.is_file():
                return path
        raise QueueUnavailable("Codex state database is unavailable")

    def _thread_row(self, thread_id: str) -> tuple:
        try:
            with sqlite3.connect("file:" + str(self._state_db()) + "?mode=ro", uri=True, timeout=0.5) as db:
                row = db.execute("SELECT rollout_path,model,archived FROM threads WHERE id=?", (thread_id,)).fetchone()
        except (OSError, sqlite3.Error):
            raise QueueUnavailable("Codex state database is unavailable") from None
        if not row or row[2]:
            raise QueueUnavailable("Codex session is unavailable")
        return row

    @staticmethod
    def _rollout_snapshot(path: Path, thread_id: str, model: str) -> Snapshot:
        latest_id = latest_status = latest_token = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        payload = json.loads(line).get("payload", {})
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(payload, dict) or payload.get("thread_id") not in (None, thread_id):
                        continue
                    kind = payload.get("type")
                    if kind == "task_started" and isinstance(payload.get("turn_id"), str):
                        latest_id, latest_status, latest_token = payload["turn_id"], "inProgress", None
                    elif kind == "task_complete" and payload.get("turn_id") == latest_id:
                        latest_status = "failed" if payload.get("error") else "completed"
                    elif kind in {"message", "item_completed"} and latest_id:
                        item = payload if kind == "message" else payload.get("item", {})
                        if kind == "message" and item.get("role") != "user":
                            continue
                        if kind == "item_completed" and item.get("type") != "UserMessage":
                            continue
                        text = "".join(item.get("text", "") for item in item.get("content", []) if isinstance(item, dict) and isinstance(item.get("text"), str))
                        match = re.search(re.escape(MESSAGE_PREFIX) + r"([a-f0-9-]{36})\]", text)
                        if match:
                            latest_token = match.group(1)
        except (OSError, UnicodeError):
            raise QueueUnavailable("Codex session history is unavailable") from None
        if not latest_id or not latest_status:
            raise QueueUnavailable("Codex session history is unavailable")
        return Snapshot(latest_id, latest_status, model or "", latest_token, True, False)

    def inspect(self, thread_id: str) -> Snapshot:
        rollout, model, _ = self._thread_row(thread_id)
        return self._rollout_snapshot(Path(rollout), thread_id, model)

    def resume(self, thread_id: str, message: str, model: str) -> str | None:
        # Deliberately omit --model: Codex queue uses the persisted session model.
        try:
            result = subprocess.run([self.codex_bin, "queue", "--thread", thread_id, "--message", message], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60, env=os.environ.copy())
        except subprocess.TimeoutExpired:
            raise QueueOutcomeUnknown("Codex queue request timed out") from None
        except OSError:
            raise QueueUnavailable("Codex queue is unavailable") from None
        if result.returncode:
            raise QueueUnavailable("Codex queue rejected the continuation")
        return None

    def diagnose(self, thread_id: str | None = None) -> dict:
        result = {"connection": "ready", "backend": "queue", "delivery_tested": False, "codex_bin": self.codex_bin}
        if thread_id:
            try:
                snapshot = self.inspect(thread_id)
                result.update(latest_turn_status=snapshot.status, model_known=bool(snapshot.model))
            except Exception as error:
                result.update(connection="unavailable", reason=type(error).__name__)
        return result

    def close(self) -> None:
        return None

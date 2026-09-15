"""Read the current conversation, then resume through its existing owner."""

from __future__ import annotations

import re
import stat
from pathlib import Path

from .config import Settings, codex_home, find_codex
from .recovery import MESSAGE_PREFIX, Snapshot
from .transport import AppServerClient, TransportError


class BackendUnavailable(TransportError):
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
            # A custom socket explicitly selects app-server. Otherwise prioritize
            # the desktop owner; do not try an unrelated server after a write.
            kind = "app-server" if self.settings.socket_path else "desktop"
        if kind == "desktop":
            from .desktop import DesktopClient
            self.client = DesktopClient(self.settings.socket_path, timeout=10)
        else:
            self.client = AppServerClient(self.settings.codex_bin or find_codex(), self.settings.socket_path, timeout=10)
        self.kind = kind
        return self.client

    def inspect(self, thread_id: str) -> Snapshot:
        try:
            thread = self._connect().read_thread(thread_id)
            result = snapshot_from_thread(thread)
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
            kind = "app-server" if self.settings.socket_path else "desktop"
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

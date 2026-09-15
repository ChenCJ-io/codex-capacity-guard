"""Local desktop owner transport for Codex Capacity Guard.

This is an experimental adapter for the desktop's private IPC protocol. It
connects to an existing owner; it never starts a router, takes thread ownership,
edits Codex state, handles approvals, or supplies model/permission overrides.
The desktop may change this protocol independently of the public app-server.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import time
from typing import Any
from uuid import uuid4

from .transport import TransportError, TransportUnavailable


MAX_FRAME_BYTES = 256 * 1024 * 1024
SNAPSHOT_VERSION = 11


class DesktopUnavailable(TransportUnavailable):
    """No continuation was submitted to a desktop owner."""

    outcome_unknown = False


class DesktopOutcomeUnknown(TransportError):
    """Continuation bytes may have reached the owner; inspect before retrying."""

    outcome_unknown = True


class _NotDispatched(DesktopUnavailable):
    """The router confirms the continuation never reached an owner."""


def _socket_path(value: str | Path | None) -> Path:
    if value is None:
        codex_directory = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        return codex_directory / "ipc" / "ipc.sock"
    return Path(value).expanduser()


def _validate_socket(path: Path) -> None:
    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"):
        raise DesktopUnavailable("Desktop IPC requires a local Unix socket")
    try:
        parent, endpoint = path.parent.lstat(), path.lstat()
    except OSError:
        raise DesktopUnavailable("Existing desktop IPC socket is unavailable") from None
    if (
        not stat.S_ISDIR(parent.st_mode)
        or not stat.S_ISSOCK(endpoint.st_mode)
        or parent.st_uid != os.getuid()
        or endpoint.st_uid != os.getuid()
        or parent.st_mode & 0o022
        or endpoint.st_mode & 0o077
    ):
        raise DesktopUnavailable("Desktop IPC socket ownership or permissions are unsafe")


class _Connection:
    def __init__(self, path: Path, timeout: float):
        _validate_socket(path)
        self.timeout = timeout
        self.client_id = "initializing-client"
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.settimeout(timeout)
            self.socket.connect(str(path))
            response = self.request("initialize", {"clientType": "codex-capacity-guard"}, 0)
            self.client_id = response["result"]["clientId"]
            if not isinstance(self.client_id, str) or not self.client_id:
                raise DesktopUnavailable("Invalid desktop IPC initialization result")
        except (OSError, KeyError, TypeError, TransportError):
            self.close()
            raise DesktopUnavailable("Could not initialize existing desktop IPC") from None

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not payload or len(payload) > MAX_FRAME_BYTES:
            raise DesktopUnavailable("Desktop IPC request exceeds the frame limit")
        try:
            self.socket.sendall(struct.pack("<I", len(payload)) + payload)
        except OSError:
            raise DesktopUnavailable("Desktop IPC connection was lost") from None

    def _read_exact(self, length: int, deadline: float) -> bytes:
        result = bytearray()
        while len(result) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DesktopUnavailable("Desktop IPC response timed out")
            self.socket.settimeout(remaining)
            try:
                chunk = self.socket.recv(min(length - len(result), 65536))
            except OSError:
                raise DesktopUnavailable("Desktop IPC response is unavailable") from None
            if not chunk:
                raise DesktopUnavailable("Desktop IPC connection was closed")
            result.extend(chunk)
        return bytes(result)

    def _receive(self, deadline: float) -> dict[str, Any]:
        length = struct.unpack("<I", self._read_exact(4, deadline))[0]
        if not 0 < length <= MAX_FRAME_BYTES:
            raise DesktopUnavailable("Desktop IPC returned an invalid frame length")
        try:
            message = json.loads(self._read_exact(length, deadline))
        except (ValueError, UnicodeError):
            raise DesktopUnavailable("Desktop IPC returned an invalid message") from None
        if not isinstance(message, dict):
            raise DesktopUnavailable("Desktop IPC returned an invalid message")
        if message.get("type") == "client-discovery-request":
            self._send({
                "type": "client-discovery-response", "requestId": message.get("requestId"),
                "response": {"canHandle": False},
            })
        elif message.get("type") == "request":
            # The guard cannot handle tools, approvals, or thread ownership.
            self._send({
                "type": "response", "requestId": message.get("requestId"),
                "resultType": "error", "error": "no-handler-for-request",
            })
        return message

    def request(
        self, method: str, params: dict[str, Any], version: int,
        target: str | None = None, *, mutation: bool = False,
    ) -> dict[str, Any]:
        request_id = str(uuid4())
        payload = {
            "type": "request", "requestId": request_id,
            "sourceClientId": self.client_id, "method": method,
            "version": version, "params": params, "timeoutMs": int(self.timeout * 1000),
        }
        if target is not None:
            payload["targetClientId"] = target
        try:
            self._send(payload)
            deadline = time.monotonic() + self.timeout
            while True:
                response = self._receive(deadline)
                if response.get("type") != "response" or response.get("requestId") != request_id:
                    continue
                if response.get("resultType") != "success":
                    # These router errors prove that no owner handled the request.
                    if response.get("error") in {
                        "no-client-found", "request-version-mismatch", "no-handler-for-request"
                    }:
                        raise _NotDispatched("No compatible desktop owner is available")
                    if mutation:
                        raise DesktopOutcomeUnknown("Desktop continuation outcome is unknown")
                    raise DesktopUnavailable("Desktop owner could not complete the request")
                if response.get("method") != method or not isinstance(response.get("result"), dict):
                    raise DesktopUnavailable("Invalid desktop IPC response")
                return response
        except DesktopUnavailable as error:
            if mutation and not isinstance(error, _NotDispatched):
                raise DesktopOutcomeUnknown("Desktop continuation outcome is unknown") from None
            raise

    def snapshot(self, thread_id: str) -> tuple[str, dict[str, Any]]:
        response = self.request(
            "thread-owner-discovery", {"hostId": "local", "conversationId": thread_id}, 1,
        )
        owner = response.get("handledByClientId")
        if not isinstance(owner, str) or not owner:
            raise DesktopUnavailable("Desktop thread has no available owner")
        self.follow(thread_id, owner, True)
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                message = self._receive(deadline)
                params = message.get("params")
                if (
                    message.get("type") != "broadcast"
                    or message.get("method") != "thread-stream-state-changed"
                    or message.get("sourceClientId") != owner
                    or not isinstance(params, dict)
                    or params.get("hostId") != "local"
                    or params.get("conversationId") != thread_id
                ):
                    continue
                if message.get("version") != SNAPSHOT_VERSION:
                    raise DesktopUnavailable("Unsupported desktop thread snapshot protocol")
                change = params.get("change")
                if not isinstance(change, dict) or change.get("type") != "snapshot":
                    continue
                state = change.get("conversationState")
                if not isinstance(state, dict) or state.get("id") != thread_id:
                    raise DesktopUnavailable("Invalid desktop thread snapshot")
                return owner, _normalize_state(state)
        finally:
            try:
                self.follow(thread_id, owner, False)
            except TransportError:
                pass

    def follow(self, thread_id: str, owner: str, following: bool) -> None:
        self._send({
            "type": "broadcast", "method": "thread-stream-following-changed",
            "version": 1, "sourceClientId": self.client_id, "targetClientIds": [owner],
            "params": {"hostId": "local", "conversationId": thread_id, "following": following},
        })


def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Retain routing state and recovery markers, without tool/assistant outputs."""
    try:
        turns = state.get("turns", [])
        history = state.get("turnHistory")
        if isinstance(history, dict) and history.get("kind") == "canonical":
            canonical = history["history"]
            turns = [
                canonical["entitiesByKey"][entry["value"]]
                for island in canonical["islands"] for entry in island["entries"]
            ]
        if not isinstance(turns, list):
            raise ValueError
        normalized = []
        for turn in turns:
            if not isinstance(turn, dict):
                raise ValueError
            params = turn.get("params", {})
            if not isinstance(params, dict):
                raise ValueError
            # The opening prompt is in params.input even before item hydration.
            inputs = params.get("input", [])
            texts = [item["text"] for item in inputs if isinstance(item, dict)
                     and item.get("type") == "text" and isinstance(item.get("text"), str)]
            normalized.append({
                "id": turn.get("turnId"), "status": turn.get("status"),
                "items": [{"type": "userMessage", "content": [
                    {"type": "text", "text": text} for text in texts
                ]}] if texts else [],
            })
        settings = state.get("latestThreadSettings") or {}
        collaboration = settings.get("collaborationMode") or state.get("latestCollaborationMode") or {}
        model = (collaboration.get("settings") or {}).get("model") or settings.get("model") or state.get("latestModel")
        runtime = state.get("threadRuntimeStatus")
        if not isinstance(runtime, dict) or not isinstance(runtime.get("type"), str):
            raise ValueError
        return {
            "id": state["id"], "status": runtime, "turns": normalized,
            "model": model if isinstance(model, str) else "",
            "hasDraft": None,  # Composer drafts are not exposed by this protocol.
            "pendingSubmissions": bool(state.get("unconfirmedTurnSubmissions")),
            "pendingApprovals": bool(state.get("requests")),
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        raise DesktopUnavailable("Unsupported desktop thread snapshot shape") from None


class DesktopClient:
    """Use the running desktop owner, preserving its settings by omission.

    Each operation uses a fresh connection, so a read cannot consume a stale
    snapshot from a previous subscription. No arbitrary IPC request API is
    exposed. ``start_turn`` checks a fresh snapshot immediately before sending;
    the private protocol does not offer an atomic expected-turn precondition.
    """

    def __init__(self, socket_path: str | Path | None = None, timeout: float = 15):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.socket_path, self.timeout = _socket_path(socket_path), timeout
        self._closed = False
        self._last_turn: dict[str, str | None] = {}

    def __enter__(self) -> DesktopClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        self._last_turn.clear()

    def _connect(self) -> _Connection:
        if self._closed:
            raise DesktopUnavailable("Desktop client is closed")
        return _Connection(self.socket_path, self.timeout)

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id is required")
        with self._connect() as connection:
            _, thread = connection.snapshot(thread_id)
        self._last_turn[thread_id] = thread["turns"][-1]["id"] if thread["turns"] else None
        return thread

    def start_turn(
        self, thread_id: str, message: str, model: str | None = None,
        *, expected_turn_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(thread_id, str) or not thread_id or not isinstance(message, str) or not message.strip():
            raise ValueError("thread_id and message are required")
        if model is not None and (not isinstance(model, str) or not model):
            raise ValueError("model must be a nonempty string")
        expected = expected_turn_id or self._last_turn.get(thread_id)
        if expected is None:
            raise DesktopUnavailable("Inspect the failed turn before requesting continuation")
        with self._connect() as connection:
            owner, thread = connection.snapshot(thread_id)
            turns = thread["turns"]
            if (
                thread["status"]["type"] != "idle" or thread["pendingSubmissions"]
                or thread["pendingApprovals"] or not turns
                or turns[-1]["id"] != expected or turns[-1]["status"] != "failed"
                or not thread["model"] or (model is not None and thread["model"] != model)
            ):
                raise DesktopUnavailable("Desktop thread changed or is not ready for continuation")
            response = connection.request(
                "thread-follower-start-turn",
                {"conversationId": thread_id, "turnStart": {
                    "request": {
                        "threadId": thread_id, "clientUserMessageId": str(uuid4()),
                        "input": [{"type": "text", "text": message, "text_elements": []}],
                    },
                    "context": {"inheritThreadSettings": True},
                }},
                2, target=owner, mutation=True,
            )
            result = response["result"].get("result")
            if not isinstance(result, dict) or not isinstance(result.get("turn"), dict):
                raise DesktopOutcomeUnknown("Invalid desktop continuation confirmation")
            turn = result["turn"]
            if not isinstance(turn.get("id"), str) or not turn["id"]:
                raise DesktopOutcomeUnknown("Invalid desktop continuation confirmation")
            return {"turn": {"id": turn["id"], "status": turn.get("status")}}

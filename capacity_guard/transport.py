"""JSON-RPC over WebSocket on an existing local App Server Unix socket.

This client never starts a server, resumes a stored thread, answers approvals,
or logs request/response bodies. A socket path must be selected explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import threading
from dataclasses import dataclass, field
from typing import Any


MAX_MESSAGE = 64 * 1024 * 1024


class TransportError(RuntimeError):
    outcome_unknown = True


class TransportUnavailable(TransportError):
    """No usable connection. During a write the outcome may still be unknown."""


class RequestTimeout(TransportError):
    def __init__(self, method: str):
        self.method = method
        super().__init__(f"App-server request timed out: {method}; outcome unknown")


class RpcError(TransportError):
    def __init__(self, method: str, code: int | None):
        self.method, self.code = method, code
        super().__init__(f"App-server rejected {method} (code {code})")


@dataclass
class _Pending:
    method: str
    ready: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    error: TransportError | None = None


class AppServerClient:
    """A single WebSocket reader dispatches replies to concurrent callers."""

    def __init__(self, codex_bin: str, socket_path: str | None = None, timeout: float = 10):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if not socket_path:
            raise TransportUnavailable("An existing app-server socket path is required")
        # codex_bin is accepted for API compatibility; no child process is started.
        self.timeout = timeout
        self._lock, self._write_lock = threading.Lock(), threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._closed = False
        self._failure = None
        self._reader = None
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            path = Path(socket_path)
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise TransportUnavailable("Unsafe app-server socket")
            self._socket.settimeout(timeout)
            self._socket.connect(str(path))
            self._handshake()
            self._socket.settimeout(None)
            self._reader = threading.Thread(target=self._read_messages, name="capacity-guard-rpc", daemon=True)
            self._reader.start()
            self.request("initialize", {
                "clientInfo": {"name": "capacity_guard", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            self._send_json({"method": "initialized"})
        except (OSError, ValueError, TransportError):
            self.close()
            raise TransportUnavailable("Could not connect to the existing app-server") from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _exact(self, count: int) -> bytes:
        output = bytearray()
        while len(output) < count:
            part = self._socket.recv(min(count - len(output), 65536))
            if not part:
                raise TransportUnavailable("App-server closed the connection")
            output.extend(part)
        return bytes(output)

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Key: {key}\r\n\r\n")
        self._socket.sendall(request.encode("ascii"))
        response = bytearray()
        while not response.endswith(b"\r\n\r\n"):
            if len(response) >= 16384:
                raise TransportUnavailable("Invalid WebSocket handshake")
            response.extend(self._exact(1))
        lines = response.decode("ascii").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.lower()] = v.strip()
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if (len(lines[0].split()) < 2 or lines[0].split()[1] != "101"
                or headers.get("sec-websocket-accept") != accept
                or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in headers.get("connection", "").lower()):
            raise TransportUnavailable("App-server refused WebSocket upgrade")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if len(payload) > MAX_MESSAGE:
            raise ValueError("Request exceeds WebSocket message limit")
        length = len(payload)
        prefix = bytes([0x80 | opcode])
        if length < 126:
            prefix += bytes([0x80 | length])
        elif length < 65536:
            prefix += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            prefix += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self._write_lock:
            if self._closed or self._failure:
                raise TransportUnavailable("App-server connection is unavailable")
            self._socket.sendall(prefix + mask + masked)

    def _send_json(self, payload: dict) -> None:
        self._send_frame(1, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())

    def _receive_json(self) -> dict:
        payload = bytearray()
        fragmented = False
        while True:
            first, second = self._exact(2)
            final, opcode = bool(first & 0x80), first & 0x0f
            if first & 0x70 or second & 0x80:
                raise TransportUnavailable("Unsupported WebSocket frame")
            length = second & 0x7f
            if length == 126:
                length = struct.unpack("!H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._exact(8))[0]
            if length > MAX_MESSAGE or len(payload) + length > MAX_MESSAGE:
                raise TransportUnavailable("WebSocket message exceeds limit")
            if opcode >= 8 and (not final or length > 125):
                raise TransportUnavailable("Invalid WebSocket control frame")
            data = self._exact(length)
            if opcode == 8:
                raise TransportUnavailable("App-server closed WebSocket")
            if opcode == 9:
                self._send_frame(10, data)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not fragmented:
                fragmented = True
            elif opcode != 0 or not fragmented:
                raise TransportUnavailable("Unsupported WebSocket message")
            payload.extend(data)
            if final:
                result = json.loads(payload)
                if not isinstance(result, dict):
                    raise TransportUnavailable("Invalid app-server JSON response")
                return result

    def _fail(self, error: TransportError) -> None:
        with self._lock:
            self._failure = error
            for pending in self._pending.values():
                pending.error = error
                pending.ready.set()

    def _read_messages(self) -> None:
        try:
            while not self._closed:
                message = self._receive_json()
                if "method" in message:
                    # The original owner alone handles approvals/tool requests.
                    # An error reply here would steal its shared request callback.
                    continue
                response_id = message.get("id")
                if type(response_id) is not int:
                    continue
                with self._lock:
                    pending = self._pending.get(response_id)
                    if pending:
                        pending.response = message
                        pending.ready.set()
        except (OSError, ValueError, TransportError):
            self._fail(TransportUnavailable("App-server connection was lost"))

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(method, str) or not method or not isinstance(params, dict):
            raise ValueError("A method and parameter object are required")
        json.dumps(params, allow_nan=False)
        with self._lock:
            if self._closed or self._failure:
                raise TransportUnavailable("App-server connection is unavailable")
            self._next_id += 1
            request_id = self._next_id
            pending = self._pending[request_id] = _Pending(method)
        try:
            try:
                self._send_json({"id": request_id, "method": method, "params": params})
            except OSError:
                raise TransportUnavailable("App-server write outcome is unknown") from None
            if not pending.ready.wait(self.timeout):
                raise RequestTimeout(method)
            if pending.error:
                raise pending.error
            response = pending.response or {}
            if "error" in response:
                error = response["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise RpcError(method, code if type(code) is int else None)
            result = response.get("result")
            if not isinstance(result, dict):
                raise TransportError("Invalid app-server result")
            return result
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def read_thread(self, thread_id: str) -> dict:
        result = self.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        if not isinstance(result.get("thread"), dict):
            raise TransportError("Invalid app-server thread result")
        return result["thread"]

    def start_turn(self, thread_id: str, message: str, model: str | None = None) -> dict:
        if not isinstance(thread_id, str) or not thread_id or not isinstance(message, str) or not message:
            raise ValueError("thread_id and message are required")
        params = {"threadId": thread_id, "input": [{"type": "text", "text": message}]}
        if model is not None:
            if not isinstance(model, str) or not model:
                raise ValueError("model must be nonempty")
            params["model"] = model
        return self.request("turn/start", params)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        self._fail(TransportUnavailable("App-server client closed"))
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)

"""Synthetic WebSocket-over-Unix tests; never connect to a user session."""
import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from capacity_guard.transport import AppServerClient, RequestTimeout, RpcError, TransportError


def exact(stream, count):
    output = bytearray()
    while len(output) < count:
        chunk = stream.recv(count - len(output))
        if not chunk:
            raise EOFError
        output.extend(chunk)
    return bytes(output)


def receive(stream):
    first, second = exact(stream, 2)
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", exact(stream, 8))[0]
    assert second & 128, "Client messages must be masked"
    mask = exact(stream, 4)
    data = bytes(value ^ mask[i % 4] for i, value in enumerate(exact(stream, length)))
    return first & 15, data


class FakeServer:
    def __init__(self, root, bad_handshake=False):
        self.path = str(Path(root) / "app.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        Path(self.path).chmod(0o600)
        self.listener.listen()
        self.listener.settimeout(.1)
        self.stopped = threading.Event()
        self.messages, self.errors = [], []
        self.bad_handshake = bad_handshake
        self.lock = threading.Lock()
        self.stream = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def send(self, value, opcode=1, final=True):
        data = json.dumps(value).encode() if not isinstance(value, bytes) else value
        prefix = bytes([(0x80 if final else 0) | opcode])
        if len(data) < 126:
            prefix += bytes([len(data)])
        elif len(data) < 65536:
            prefix += b"\x7e" + struct.pack("!H", len(data))
        else:
            prefix += b"\x7f" + struct.pack("!Q", len(data))
        with self.lock:
            self.stream.sendall(prefix + data)

    def run(self):
        try:
            while not self.stopped.is_set():
                try:
                    self.stream, _ = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            if self.stream is None:
                return
            with self.stream:
                request = bytearray()
                while not request.endswith(b"\r\n\r\n"):
                    request.extend(exact(self.stream, 1))
                headers = dict(line.split(": ", 1) for line in request.decode().split("\r\n")[1:] if ": " in line)
                accept = base64.b64encode(hashlib.sha1((headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                if self.bad_handshake:
                    accept = "wrong"
                self.stream.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
                while not self.stopped.is_set():
                    opcode, data = receive(self.stream)
                    if opcode == 10:
                        self.messages.append({"pong": data.decode()})
                        continue
                    value = json.loads(data)
                    self.messages.append(value)
                    if "id" not in value:
                        continue
                    method, ident = value.get("method"), value["id"]
                    if method == "slow":
                        def later(i=ident):
                            time.sleep(.15)
                            try:
                                self.send({"id": i, "result": {"late": True}})
                            except OSError:
                                pass
                        threading.Thread(target=later, daemon=True).start()
                        continue
                    if method == "error":
                        self.send({"id": ident, "error": {"code": -1, "message": "private-secret"}})
                    elif method == "fragment":
                        data = json.dumps({"id": ident, "result": {"fragmented": True}}).encode()
                        self.send(data[:8], final=False)
                        self.send(b"probe", opcode=9)
                        self.send(data[8:], opcode=0)
                    elif method == "bad":
                        self.send(b"private-invalid-json")
                    elif method == "approval":
                        self.send({"method": "item/commandExecution/requestApproval", "id": "owner-approval", "params": {}})
                        self.send({"id": ident, "result": {"sent": True}})
                    elif method == "thread/read":
                        self.send({"id": ident, "result": {"thread": {"id": "t", "turns": [{"id": "old", "status": "failed"}]}}})
                    elif method == "turn/start":
                        self.send({"id": ident, "result": {"turn": {"id": "new", "status": "inProgress"}}})
                    else:
                        self.send({"method": "ignored-notification", "params": {"private": "ignored"}})
                        self.send({"id": ident, "result": value.get("params", {})})
        except (OSError, EOFError):
            pass
        except Exception as error:
            self.errors.append(error)

    def close(self):
        self.stopped.set()
        if self.stream:
            try:
                self.stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.listener.close()
        self.thread.join(timeout=2)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cg-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.server = FakeServer(self.temp.name)
        self.addCleanup(self.server.close)
        self.client = AppServerClient("unused", self.server.path, timeout=2)
        self.addCleanup(self.client.close)

    def test_real_websocket_handshake_and_thread_read(self):
        self.assertEqual("failed", self.client.read_thread("t")["turns"][0]["status"])

    def test_keep_settings_by_omission(self):
        self.client.start_turn("t", "continue")
        params = self.server.messages[-1]["params"]
        self.assertEqual({"threadId": "t", "input": [{"type": "text", "text": "continue"}]}, params)

    def test_explicit_model_has_no_fallback(self):
        self.client.start_turn("t", "continue", "original")
        self.assertEqual("original", self.server.messages[-1]["params"]["model"])

    def test_fragmented_text_and_ping(self):
        self.assertTrue(self.client.request("fragment", {})["fragmented"])
        self.client.request("barrier", {})
        self.assertIn({"pong": "probe"}, self.server.messages)

    def test_large_masked_payload(self):
        payload = {"message": "x" * 70000}
        self.assertEqual(payload, self.client.request("echo", payload))

    def test_concurrent_requests_have_matching_replies(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self.client.request("echo", {"i": i}), range(12)))
        self.assertEqual([{"i": i} for i in range(12)], results)

    def test_late_response_cannot_satisfy_next_request(self):
        self.client.timeout = .05
        with self.assertRaises(RequestTimeout):
            self.client.request("slow", {})
        self.client.timeout = 2
        self.assertEqual({"fresh": True}, self.client.request("echo", {"fresh": True}))
        time.sleep(.16)
        self.assertEqual({}, self.client.request("echo", {}))

    def test_auxiliary_connection_does_not_answer_owner_approvals(self):
        self.client.request("approval", {})
        self.client.request("barrier", {})
        self.assertFalse(any(item.get("id") == "owner-approval" for item in self.server.messages))

    def test_rpc_error_redacts_private_details(self):
        with self.assertRaises(RpcError) as ctx:
            self.client.request("error", {})
        self.assertNotIn("private", str(ctx.exception))

    def test_malformed_json_does_not_leak_content(self):
        with self.assertRaises(TransportError) as ctx:
            self.client.request("bad", {})
        self.assertNotIn("private", str(ctx.exception))

    def test_close_and_missing_socket(self):
        self.client.close()
        self.client.close()
        with self.assertRaises(TransportError):
            self.client.request("echo", {})
        with self.assertRaises(TransportError):
            AppServerClient("unused", str(Path(self.temp.name) / "missing"))
        with self.assertRaises(TransportError):
            AppServerClient("unused")

    def test_invalid_handshake_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="cg-", dir="/tmp") as root:
            server = FakeServer(root, bad_handshake=True)
            try:
                with self.assertRaises(TransportError):
                    AppServerClient("unused", server.path)
            finally:
                server.close()

    def test_invalid_request_is_never_sent(self):
        count = len(self.server.messages)
        with self.assertRaises(ValueError):
            self.client.request("echo", {"bad": float("nan")})
        with self.assertRaises(ValueError):
            self.client.start_turn("t", "")
        self.assertEqual(count, len(self.server.messages))


if __name__ == "__main__":
    unittest.main()

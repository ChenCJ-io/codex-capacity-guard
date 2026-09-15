"""Optional offline smoke using a real Codex binary and a loopback model stub.

Run from the project root:
  python3 tests/smoke_transport.py /path/to/codex

Only synthetic threads in a temporary CODEX_HOME are created. The provider
requires no authentication and its only configured endpoint is loopback.
"""

from __future__ import annotations

import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capacity_guard.transport import AppServerClient, RpcError


class ModelStub(BaseHTTPRequestHandler):
    requests_seen: list[str] = []
    fail = True

    def log_message(self, *_: object) -> None:
        pass

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        try:
            request = json.loads(body)
        except (ValueError, UnicodeError):
            request = {}
        type(self).requests_seen.append(str(request.get("model", "unknown")))
        if type(self).fail:
            payload = json.dumps({"error": {
                "message": "Selected model is at capacity. Please try a different model.",
                "type": "server_error", "code": "model_capacity_exceeded",
            }}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        message = {
            "id": "msg_smoke", "type": "message", "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": "done", "annotations": []}],
        }
        response = {
            "id": "resp_smoke", "object": "response", "status": "completed",
            "output": [message],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        events = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
            {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 0, "content_index": 0, "delta": "done"},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": response},
        ]
        payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def wait_turn(client: AppServerClient, thread_id: str, turn_id: str) -> dict:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            turns = client.read_thread(thread_id).get("turns", [])
        except RpcError as error:
            # Fresh synthetic threads can be unmaterialized for a few ticks.
            if error.code != -32603:
                raise
            time.sleep(0.05)
            continue
        for turn in turns:
            if turn.get("id") == turn_id and turn.get("status") in ("failed", "completed", "interrupted"):
                return turn
        time.sleep(0.05)
    raise AssertionError("Synthetic turn did not finish within 25 seconds")


def smoke(codex_bin: str) -> dict:
    ModelStub.requests_seen = []
    ModelStub.fail = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="cg-", dir="/tmp") as root:
            workspace = Path(root) / "workspace"
            workspace.mkdir()
            isolated = Path(root) / "codex"
            isolated.mkdir()
            socket = str(Path(root) / "app.sock")
            config = f'''
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
'''
            (isolated / "config.toml").write_text(config)
            environment = {
                key: os.environ[key]
                for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "LANG")
                if key in os.environ
            }
            environment["CODEX_HOME"] = str(isolated)
            with patch.dict(os.environ, environment, clear=True):
                process = subprocess.Popen(
                    [codex_bin, "app-server", "--listen", "unix://" + socket],
                    cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 10
                while not Path(socket).exists():
                    if process.poll() is not None:
                        raise AssertionError("Isolated app-server exited before listening")
                    if time.monotonic() >= deadline:
                        raise AssertionError("Isolated app-server did not create its socket")
                    time.sleep(0.05)
                with AppServerClient(codex_bin, socket, timeout=10) as owner:
                    created = owner.request("thread/start", {"cwd": str(workspace), "historyMode": "legacy"})
                    thread_id = created["thread"]["id"]
                    first = owner.start_turn(thread_id, "Synthetic offline capacity test.")
                    failed = wait_turn(owner, thread_id, first["turn"]["id"])
                    assert failed["status"] == "failed", "First synthetic turn must fail"
                    ModelStub.fail = False
                    with AppServerClient(codex_bin, socket, timeout=10) as recovery:
                        loaded = recovery.request("thread/loaded/list", {})
                        assert thread_id in loaded["data"], "Secondary connection must see original loaded thread"
                        retry = recovery.start_turn(thread_id, "Continue the synthetic task.")
                        completed = wait_turn(owner, thread_id, retry["turn"]["id"])
                        assert completed["status"] == "completed", "Recovery must complete on the original server"
                    assert set(ModelStub.requests_seen) == {"capacity-smoke"}, "Model must stay unchanged"
                    return {
                        "failed_turn": failed["status"], "retry_turn": completed["status"],
                        "same_loaded_thread": True, "same_model": True,
                        "local_provider_requests": len(ModelStub.requests_seen),
                    }
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 tests/smoke_transport.py /path/to/codex")
    print(json.dumps(smoke(sys.argv[1]), ensure_ascii=False))

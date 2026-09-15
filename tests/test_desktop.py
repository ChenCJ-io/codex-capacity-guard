import copy
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest

from capacity_guard.desktop import (
    DesktopClient, DesktopOutcomeUnknown, DesktopUnavailable,
    MAX_FRAME_BYTES, _normalize_state,
)


STATE = {
    "id": "thread-1", "latestModel": "original-model",
    "threadRuntimeStatus": {"type": "idle"},
    "requests": [], "unconfirmedTurnSubmissions": [],
    "turns": [{
        "turnId": "turn-failed", "status": "failed",
        "params": {"input": [{"type": "text", "text": "user task"}]},
        "items": [{"type": "commandExecution", "output": "private command output"}],
    }],
}


def read_frame(stream):
    def exact(size):
        value = bytearray()
        while len(value) < size:
            chunk = stream.recv(size - len(value))
            if not chunk:
                return None
            value.extend(chunk)
        return bytes(value)
    header = exact(4)
    if header is None:
        return None
    payload = exact(struct.unpack("<I", header)[0])
    return None if payload is None else json.loads(payload)


class FakeDesktop:
    def __init__(self, directory):
        self.path = Path(directory) / "ipc.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.path.chmod(0o600)
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.stopped = threading.Event()
        self.state = copy.deepcopy(STATE)
        self.snapshot_version = 11
        self.snapshot_style = "normal"
        self.owner_available = True
        self.start_behavior = "success"
        self.sent = []
        self.replies = []
        self.subscriptions = []
        self.errors = []
        self.fragment = True
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def send(self, stream, value):
        payload = json.dumps(value).encode()
        frame = struct.pack("<I", len(payload)) + payload
        if self.fragment:
            # Split both the length prefix and UTF-8 body across socket reads.
            for chunk in (frame[:1], frame[1:3], frame[3:17], frame[17:]):
                stream.sendall(chunk)
        else:
            stream.sendall(frame)

    def reply(self, stream, request, result=None, error=None):
        value = {"type": "response", "requestId": request["requestId"]}
        if error:
            value.update(resultType="error", error=error)
        else:
            value.update(resultType="success", method=request["method"],
                         handledByClientId="owner-1", result=result)
        self.send(stream, value)

    def run(self):
        while not self.stopped.is_set():
            try:
                stream, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with stream:
                    stream.settimeout(1)
                    self.serve(stream)
            except (OSError, ValueError):
                continue
            except BaseException as error:
                self.errors.append(error)

    def serve(self, stream):
        while not self.stopped.is_set():
            request = read_frame(stream)
            if request is None:
                return
            method = request.get("method")
            if request["type"] in {"response", "client-discovery-response"}:
                self.replies.append(request)
            elif method == "initialize":
                self.reply(stream, request, {"clientId": "guard-1"})
            elif method == "thread-owner-discovery":
                assert request["version"] == 1
                assert request["params"] == {"hostId": "local", "conversationId": "thread-1"}
                if not self.owner_available:
                    self.reply(stream, request, error="no-client-found")
                    continue
                self.send(stream, {"type": "client-discovery-request", "requestId": "discovery-probe", "request": {"method": "some-request"}})
                self.send(stream, {"type": "request", "requestId": "approval-probe", "method": "thread-follower-command-approval-decision"})
                self.reply(stream, request, {"supportsUntrustedAppInput": True})
            elif method == "thread-stream-following-changed":
                self.subscriptions.append(request)
                if not request["params"]["following"]:
                    continue
                if self.snapshot_style == "oversized":
                    stream.sendall(struct.pack("<I", MAX_FRAME_BYTES + 1))
                    continue
                if self.snapshot_style == "zero":
                    stream.sendall(struct.pack("<I", 0))
                    continue
                if self.snapshot_style == "invalid_json":
                    stream.sendall(struct.pack("<I", 8) + b"private!")
                    continue
                self.send(stream, {"type": "broadcast", "method": "unrelated-notification", "params": {"private": "ignored"}})
                value = {
                    "type": "broadcast", "method": "thread-stream-state-changed",
                    "sourceClientId": "owner-1", "targetClientIds": ["guard-1"],
                    "version": self.snapshot_version,
                    "params": {"hostId": "local", "conversationId": "thread-1", "change": {
                        "type": "snapshot", "revision": 8, "conversationState": self.state,
                    }},
                }
                if self.snapshot_style == "wrong_owner":
                    value["sourceClientId"] = "unrelated-owner"
                self.send(stream, value)
            elif method == "thread-follower-start-turn":
                self.sent.append(request)
                if self.start_behavior == "disconnect":
                    return
                if self.start_behavior == "timeout":
                    time.sleep(0.3)
                    continue
                if self.start_behavior == "private_error":
                    self.reply(stream, request, error="private sensitive error text")
                    continue
                if self.start_behavior == "no_owner":
                    self.reply(stream, request, error="no-client-found")
                    continue
                if self.start_behavior == "invalid_result":
                    self.reply(stream, request, {"result": {"missing": "turn"}})
                    continue
                self.reply(stream, request, {"result": {"turn": {"id": "turn-new", "status": "inProgress"}}})
            else:
                raise AssertionError("Unexpected desktop request")

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="capacity-desktop-")
        self.addCleanup(self.directory.cleanup)
        self.desktop = FakeDesktop(self.directory.name)
        self.addCleanup(self.desktop.close)

    def client(self, timeout=0.2):
        client = DesktopClient(self.desktop.path, timeout=timeout)
        self.addCleanup(client.close)
        return client

    def tearDown(self):
        self.assertEqual(self.desktop.errors, [])

    def test_reads_fragmented_frames_and_discards_unrelated_private_content(self):
        thread = self.client().read_thread("thread-1")
        self.assertEqual(thread["id"], "thread-1")
        self.assertEqual(thread["status"], {"type": "idle"})
        self.assertEqual(thread["turns"][0]["id"], "turn-failed")
        self.assertNotIn("private", json.dumps(thread))
        self.assertIsNone(thread["hasDraft"])
        self.assertFalse(thread["pendingApprovals"])

    def test_normalizes_canonical_turns_in_island_order(self):
        state = copy.deepcopy(STATE)
        second = copy.deepcopy(state["turns"][0])
        second.update(turnId="latest", status="failed")
        second["params"]["input"] = [{"type": "text", "text": "[codex-capacity-guard recovery=marker]"}]
        state["turnHistory"] = {"kind": "canonical", "history": {
            "entitiesByKey": {"b": second, "a": state["turns"][0]},
            "islands": [{"entries": [{"value": "a"}]}, {"entries": [{"value": "b"}]}],
        }}
        self.desktop.state = state
        thread = self.client().read_thread("thread-1")
        self.assertEqual([turn["id"] for turn in thread["turns"]], ["turn-failed", "latest"])
        self.assertIn("recovery=marker", thread["turns"][-1]["items"][0]["content"][0]["text"])

    def test_turn_start_routes_to_owner_and_inherits_without_overrides(self):
        client = self.client()
        client.read_thread("thread-1")
        result = client.start_turn("thread-1", "继续", "original-model")
        self.assertEqual(result["turn"]["id"], "turn-new")
        request = self.desktop.sent[0]
        self.assertEqual(request["targetClientId"], "owner-1")
        self.assertEqual(request["version"], 2)
        self.assertNotIn("hostId", request)
        start = request["params"]["turnStart"]
        self.assertEqual(start["context"], {"inheritThreadSettings": True})
        self.assertEqual(set(start["request"]), {"threadId", "clientUserMessageId", "input"})
        self.assertEqual(start["request"]["input"], [{"type": "text", "text": "继续", "text_elements": []}])
        self.assertEqual(len(start["request"]["clientUserMessageId"]), 36)

    def test_never_handles_approvals_or_discovery(self):
        self.client().read_thread("thread-1")
        self.assertEqual(self.desktop.replies[0]["response"], {"canHandle": False})
        self.assertEqual(self.desktop.replies[1]["resultType"], "error")
        self.assertNotIn("result", self.desktop.replies[1])

    def test_missing_owner_is_known_undelivered_and_does_not_create_one(self):
        self.desktop.owner_available = False
        with self.assertRaises(DesktopUnavailable) as caught:
            self.client().read_thread("thread-1")
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(self.desktop.sent, [])
        self.assertEqual(self.desktop.subscriptions, [])

    def test_start_requires_previously_inspected_failed_turn(self):
        with self.assertRaises(DesktopUnavailable):
            self.client().start_turn("thread-1", "continue", "original-model")
        self.assertEqual(self.desktop.sent, [])

    def test_explicit_expected_turn_allows_fresh_client(self):
        result = self.client().start_turn("thread-1", "continue", "original-model", expected_turn_id="turn-failed")
        self.assertEqual(result["turn"]["id"], "turn-new")

    def test_turn_advance_between_inspection_and_send_prevents_write(self):
        client = self.client()
        client.read_thread("thread-1")
        self.desktop.state["turns"][-1]["turnId"] = "user-advanced"
        with self.assertRaises(DesktopUnavailable) as caught:
            client.start_turn("thread-1", "continue", "original-model")
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(self.desktop.sent, [])

    def test_active_completed_interrupted_or_unknown_turn_prevents_write(self):
        for status in ("completed", "interrupted", "inProgress", "unknown"):
            with self.subTest(status=status):
                self.desktop.state["turns"][-1]["status"] = status
                with self.assertRaises(DesktopUnavailable):
                    self.client().start_turn("thread-1", "continue", "original-model", expected_turn_id="turn-failed")
        self.assertEqual(self.desktop.sent, [])

    def test_pending_submissions_approvals_active_runtime_or_model_change_prevents_write(self):
        cases = [
            {"unconfirmedTurnSubmissions": [{"requestId": "pending"}]},
            {"requests": [{"type": "approval"}]},
            {"threadRuntimeStatus": {"type": "active", "activeFlags": []}},
            {"latestModel": "changed-model"}, {"latestModel": None},
            {"latestThreadSettings": {"model": "changed-model"}},
            {"latestCollaborationMode": {"settings": {"model": "changed-model"}}},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                self.desktop.state = {**copy.deepcopy(STATE), **fields}
                with self.assertRaises(DesktopUnavailable):
                    self.client().start_turn("thread-1", "continue", "original-model", expected_turn_id="turn-failed")
        self.assertEqual(self.desktop.sent, [])

    def test_delivery_failure_is_unknown_and_never_retried_internally(self):
        for behavior in ("disconnect", "private_error", "invalid_result", "timeout"):
            with self.subTest(behavior=behavior):
                self.desktop.start_behavior = behavior
                client = self.client(timeout=0.1)
                with self.assertRaises(DesktopOutcomeUnknown) as caught:
                    client.start_turn("thread-1", "continue", "original-model", expected_turn_id="turn-failed")
                self.assertTrue(caught.exception.outcome_unknown)
                self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(self.desktop.sent), 4)

    def test_router_confirmed_no_owner_is_known_undelivered(self):
        self.desktop.start_behavior = "no_owner"
        with self.assertRaises(DesktopUnavailable) as caught:
            self.client().start_turn("thread-1", "continue", "original-model", expected_turn_id="turn-failed")
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(len(self.desktop.sent), 1)

    def test_invalid_or_unknown_frames_fail_without_payload_disclosure(self):
        for style in ("oversized", "zero", "invalid_json", "wrong_owner"):
            with self.subTest(style=style):
                self.desktop.snapshot_style = style
                with self.assertRaises(DesktopUnavailable) as caught:
                    self.client(timeout=0.1).read_thread("thread-1")
                self.assertNotIn("private", str(caught.exception))

    def test_unknown_snapshot_version_fails_closed(self):
        self.desktop.snapshot_version = 12
        with self.assertRaises(DesktopUnavailable):
            self.client().read_thread("thread-1")

    def test_unsafe_socket_permissions_or_missing_socket_refused(self):
        self.desktop.path.chmod(0o666)
        with self.assertRaises(DesktopUnavailable):
            self.client().read_thread("thread-1")
        with self.assertRaises(DesktopUnavailable):
            DesktopClient(self.desktop.path.parent / "missing.sock").read_thread("thread-1")

    def test_invalid_snapshot_shape_is_not_mistaken_for_idle(self):
        for state in ({}, {**STATE, "turns": None}, {**STATE, "turns": ["invalid"]}, {**STATE, "threadRuntimeStatus": None}):
            with self.subTest(state=state), self.assertRaises(DesktopUnavailable):
                _normalize_state(state)

    def test_close_is_idempotent_and_arguments_are_validated(self):
        client = self.client()
        client.close()
        client.close()
        with self.assertRaises(DesktopUnavailable):
            client.read_thread("thread-1")
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.client(timeout=timeout)
        for args in (("", "continue"), ("thread-1", ""), ("thread-1", "  ")):
            with self.assertRaises(ValueError):
                self.client().start_turn(*args)


if __name__ == "__main__":
    unittest.main()

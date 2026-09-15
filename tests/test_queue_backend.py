import json
import os
import sqlite3
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

from capacity_guard.backends import QueueBackend, QueueOutcomeUnknown, QueueUnavailable
from capacity_guard.config import Settings
from capacity_guard.recovery import recovery_message


THREAD = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"


class QueueBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.db_path = self.home / "state_1.sqlite"
        self.rollout = self.home / "rollout.jsonl"
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, model TEXT, archived INTEGER)")
            db.execute("INSERT INTO threads VALUES (?,?,?,0)", (THREAD, str(self.rollout), "gpt-5.6-sol"))
        self.rollout.write_text("".join([
            json.dumps({"payload": {"type": "task_started", "thread_id": THREAD, "turn_id": TURN}}) + "\n",
            json.dumps({"payload": {"type": "task_complete", "thread_id": THREAD, "turn_id": TURN, "error": {"message": "Selected model is at capacity."}}}) + "\n",
        ]))
        self.script = self.root / "fake-codex"
        self.script.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_QUEUE_ARGS\"\n")
        self.script.chmod(0o700)
        self.env = {"CODEX_HOME": str(self.home), "FAKE_QUEUE_ARGS": str(self.root / "args")}

    def tearDown(self):
        self.temp.cleanup()

    def backend(self):
        return QueueBackend(Settings(codex_bin=str(self.script)))

    def test_inspect_reads_same_model_failed_turn(self):
        with mock.patch.dict(os.environ, self.env, clear=False):
            snapshot = self.backend().inspect(THREAD)
        self.assertEqual((TURN, "failed", "gpt-5.6-sol"), (snapshot.turn_id, snapshot.status, snapshot.model))

    def test_resume_uses_queue_and_does_not_pass_model(self):
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.backend().resume(THREAD, recovery_message("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), "gpt-5.6-sol")
        self.assertEqual(["queue", "--thread", THREAD, "--message"], Path(self.env["FAKE_QUEUE_ARGS"]).read_text().splitlines()[:4])
        self.assertNotIn("gpt-5.6-sol", Path(self.env["FAKE_QUEUE_ARGS"]).read_text())

    def test_queue_nonzero_is_definite_unavailable(self):
        self.script.write_text("#!/bin/sh\nexit 9\n")
        self.script.chmod(0o700)
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(QueueUnavailable) as ctx:
                self.backend().resume(THREAD, "continue", "gpt-5.6-sol")
        self.assertFalse(getattr(ctx.exception, "outcome_unknown", True))

    def test_queue_timeout_is_ambiguous(self):
        self.script.write_text("#!/bin/sh\nsleep 2\n")
        self.script.chmod(0o700)
        # Keep test fast by patching subprocess timeout behavior through a tiny
        # settings-independent fake; production remains bounded at 60 seconds.
        with mock.patch.dict(os.environ, self.env, clear=False), mock.patch('capacity_guard.backends.subprocess.run', side_effect=__import__('subprocess').TimeoutExpired('queue', 1)):
            with self.assertRaises(QueueOutcomeUnknown):
                self.backend().resume(THREAD, "continue", "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()

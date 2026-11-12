import json
import os
import sqlite3
import tempfile
import textwrap
import unittest
from contextlib import closing
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
        self.assertTrue(snapshot.eligible)

    def set_source(self, source):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("ALTER TABLE threads ADD COLUMN source TEXT")
            db.execute("UPDATE threads SET source=? WHERE id=?", (source, THREAD))

    def test_subagent_with_deleted_history_is_ineligible(self):
        self.set_source(json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": TURN}}}))
        self.rollout.unlink()
        with mock.patch.dict(os.environ, self.env, clear=False):
            snapshot = self.backend().inspect(THREAD)
        self.assertFalse(snapshot.eligible)
        self.assertEqual("gpt-5.6-sol", snapshot.model)

    def test_archived_thread_with_deleted_history_is_ineligible(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE threads SET archived=1 WHERE id=?", (THREAD,))
        self.rollout.unlink()
        with mock.patch.dict(os.environ, self.env, clear=False):
            snapshot = self.backend().inspect(THREAD)
        self.assertFalse(snapshot.eligible)

    def test_cli_and_vscode_sources_remain_eligible(self):
        self.set_source("cli")
        for source in ("cli", "vscode", json.dumps("cli")):
            with self.subTest(source=source):
                with closing(sqlite3.connect(self.db_path)) as db, db:
                    db.execute("UPDATE threads SET source=? WHERE id=?", (source, THREAD))
                with mock.patch.dict(os.environ, self.env, clear=False):
                    snapshot = self.backend().inspect(THREAD)
                self.assertTrue(snapshot.eligible)
                self.assertEqual((TURN, "failed", "gpt-5.6-sol"), (snapshot.turn_id, snapshot.status, snapshot.model))

    def test_missing_thread_is_unavailable(self):
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(QueueUnavailable):
                self.backend().inspect("33333333-3333-4333-8333-333333333333")

    def test_turn_abort_is_interrupted_and_preserves_latest_turn(self):
        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"payload": {"type": "turn_aborted", "thread_id": THREAD, "turn_id": TURN, "reason": "interrupted"}}) + "\n")
        with mock.patch.dict(os.environ, self.env, clear=False):
            snapshot = self.backend().inspect(THREAD)
        self.assertEqual((TURN, "interrupted", "gpt-5.6-sol"), (snapshot.turn_id, snapshot.status, snapshot.model))

    def test_abort_for_another_turn_does_not_override_latest_turn(self):
        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"payload": {"type": "turn_aborted", "thread_id": THREAD, "turn_id": "33333333-3333-4333-8333-333333333333"}}) + "\n")
        with mock.patch.dict(os.environ, self.env, clear=False):
            snapshot = self.backend().inspect(THREAD)
        self.assertEqual((TURN, "failed"), (snapshot.turn_id, snapshot.status))

    def test_resume_uses_queue_and_does_not_pass_model(self):
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.backend().resume(THREAD, recovery_message("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), "gpt-5.6-sol")
        self.assertEqual(["queue", "--thread", THREAD, "--message"], Path(self.env["FAKE_QUEUE_ARGS"]).read_text().splitlines()[:4])
        self.assertNotIn("gpt-5.6-sol", Path(self.env["FAKE_QUEUE_ARGS"]).read_text())

    def test_delivery_token_requires_the_complete_message_prefix(self):
        token = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        message = recovery_message(token)
        for text, expected in ((message, token), ("Please explain " + message, None), (message.replace("]\n", "] ", 1), None)):
            with self.subTest(text=text):
                self.rollout.write_text("".join(json.dumps(record) + "\n" for record in [
                    {"type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}},
                ]))
                with mock.patch.dict(os.environ, self.env, clear=False):
                    self.assertEqual(expected, self.backend().inspect(THREAD).recovery_token)

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

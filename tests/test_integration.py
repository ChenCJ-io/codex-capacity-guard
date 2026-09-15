"""Offline end-to-end: SQLite error -> waiting -> actual socket -> same thread."""

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from capacity_guard.backends import CodexBackend, snapshot_from_thread
from capacity_guard.config import Settings
from capacity_guard.log_source import LogSource
from capacity_guard.recovery import RecoveryEngine
from capacity_guard.state import Store
from test_desktop import FakeDesktop, STATE
from test_log_source import THREAD, TURN, failure_body


class IntegrationTests(unittest.TestCase):
    def test_full_recovery_and_manual_continuation_through_socket(self):
        # The desktop fake is intentionally addressed by its synthetic thread-1;
        # production errors still pass strict UUID validation at the log boundary.
        with tempfile.TemporaryDirectory(prefix="cg-") as temp:
            directory = Path(temp)
            desktop = FakeDesktop(directory)
            self.addCleanup(desktop.close)
            backend = CodexBackend(Settings(backend="desktop", socket_path=str(desktop.path), jitter=0))
            self.addCleanup(backend.close)
            store = Store(directory / "state")
            self.addCleanup(store.close)
            store.set("enabled", True)
            now = [1000.0]
            engine = RecoveryEngine(store, backend, Settings(jitter=0), lambda: now[0], lambda: .5)
            path = directory / "logs_2.sqlite"
            with sqlite3.connect(path) as writer:
                writer.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,target TEXT,feedback_log_body TEXT,thread_id TEXT)")
                writer.execute("INSERT INTO logs VALUES (1,1000,0,?,?,?)", ("codex_core::session::turn", failure_body(model="original-model"), THREAD))
            writer.close()
            events, _ = LogSource(path).read_after(0)
            self.assertEqual(1, len(events))
            # Translate only the test address after exercising the strict reader.
            from dataclasses import replace
            event = replace(events[0], thread_id="thread-1", turn_id="turn-failed")
            engine.observe(event)
            engine.tick()
            self.assertEqual([], desktop.sent)
            now[0] += 30
            engine.tick()
            self.assertEqual(1, len(desktop.sent))
            sent = desktop.sent[0]["params"]["turnStart"]
            self.assertTrue(sent["context"]["inheritThreadSettings"])
            self.assertNotIn("model", sent["request"])
            token = store.recovery("thread-1")["token"]
            from capacity_guard.recovery import recovery_message
            desktop.state["turns"].append({"turnId": "turn-new", "status": "completed", "params": {"input": [{"type": "text", "text": recovery_message(token)}]}})
            now[0] += 2
            engine.tick()
            self.assertEqual("recovered", store.recovery("thread-1")["status"])
            self.assertEqual(1, len(desktop.sent))
            self.assertEqual([], desktop.errors)

    def test_unavailable_owner_keeps_waiting_without_sending(self):
        with tempfile.TemporaryDirectory(prefix="cg-") as temp:
            desktop = FakeDesktop(temp)
            self.addCleanup(desktop.close)
            desktop.owner_available = False
            backend = CodexBackend(Settings(backend="desktop", socket_path=str(desktop.path)))
            self.addCleanup(backend.close)
            with self.assertRaises(Exception):
                backend.inspect("thread-1")
            self.assertEqual([], desktop.sent)

    def test_pending_approval_and_subagent_are_ineligible(self):
        thread = {"status": {"type": "idle"}, "turns": [{"id": TURN, "status": "failed"}], "pendingApprovals": True}
        self.assertEqual("inProgress", snapshot_from_thread(thread).status)
        thread["source"] = {"subAgent": {"thread_spawn": {}}}
        self.assertFalse(snapshot_from_thread(thread).eligible)


if __name__ == "__main__":
    unittest.main()

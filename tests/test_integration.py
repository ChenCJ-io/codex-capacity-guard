"""Offline end-to-end: SQLite error -> waiting -> actual socket -> same thread."""

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from capacity_guard.backends import CodexBackend, snapshot_from_thread
from capacity_guard.config import Settings
from capacity_guard.log_source import LogSource
from capacity_guard.recovery import RecoveryEngine
from capacity_guard.state import Store
from test_desktop import FakeDesktop
from test_log_source import THREAD, TURN, failure_body, traced_failure_body


class IntegrationTests(unittest.TestCase):
    def test_traced_capacity_errors_retry_through_queue_and_confirm_delivery(self):
        with tempfile.TemporaryDirectory(prefix="cg-") as temp:
            directory = Path(temp)
            rollout = directory / "rollout.jsonl"
            with closing(sqlite3.connect(directory / "state_5.sqlite")) as state, state:
                state.execute("CREATE TABLE threads(id TEXT,rollout_path TEXT,model TEXT,archived INTEGER,source TEXT)")
                state.execute("INSERT INTO threads VALUES (?,?,?,0,'cli')", (THREAD, str(rollout), "original-model"))
            rollout.write_text("".join(json.dumps({"type": "event_msg", "payload": payload}) + "\n" for payload in [
                {"type": "task_started", "turn_id": TURN},
                {"type": "task_complete", "turn_id": TURN, "error": {"message": "synthetic capacity failure"}},
            ]))
            with closing(sqlite3.connect(directory / "logs_2.sqlite")) as writer:
                writer.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,target TEXT,feedback_log_body TEXT,thread_id TEXT)")
                store = Store(directory / "guard")
                self.addCleanup(store.close)
                store.set("enabled", True)
                settings = Settings(backend="queue", codex_bin="synthetic-codex", jitter=0)
                backend = CodexBackend(settings)
                self.addCleanup(backend.close)
                now = [1000.0]
                engine = RecoveryEngine(store, backend, settings, lambda: now[0], lambda: .5)
                source = LogSource(directory / "logs_2.sqlite")
                cursor = 0
                commands = []
                next_turns = ["33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"]

                def observe_failure(turn):
                    nonlocal cursor
                    writer.execute("INSERT INTO logs(ts,ts_nanos,target,feedback_log_body,thread_id) VALUES (?,0,?,?,?)", (
                        int(now[0]), "codex_core::session::turn",
                        traced_failure_body(turn=turn, model='"original-model"'), THREAD,
                    ))
                    writer.commit()
                    events, cursor = source.read_after(cursor)
                    self.assertEqual(1, len(events))
                    engine.observe(events[0])

                def queue(command, **_):
                    commands.append(command)
                    turn = next_turns[len(commands) - 1]
                    message = command[command.index("--message") + 1]
                    completion = {"type": "task_complete", "turn_id": turn}
                    if len(commands) == 1:
                        completion["error"] = {"message": "synthetic capacity failure"}
                    records = [
                        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
                        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": message}]}},
                        {"type": "event_msg", "payload": completion},
                    ]
                    with rollout.open("a") as stream:
                        for record in records:
                            stream.write(json.dumps(record) + "\n")
                    return subprocess.CompletedProcess(command, 0, "", "")

                with patch.dict(os.environ, {"CODEX_HOME": str(directory)}), patch("capacity_guard.backends.subprocess.run", side_effect=queue):
                    observe_failure(TURN)
                    engine.tick()
                    self.assertEqual([], commands)
                    now[0] += 30
                    engine.tick()
                    self.assertEqual(1, len(commands))
                    now[0] += 2
                    engine.tick()
                    self.assertEqual("submitted", store.recovery(THREAD)["status"])
                    self.assertEqual(1, len(commands))
                    observe_failure(next_turns[0])
                    self.assertEqual(now[0] + 60, store.recovery(THREAD)["next_at"])
                    now[0] += 59
                    engine.tick()
                    self.assertEqual(1, len(commands))
                    now[0] += 1
                    engine.tick()
                    now[0] += 2
                    engine.tick()
                    self.assertEqual("recovered", store.recovery(THREAD)["status"])
                    self.assertEqual(2, store.recovery(THREAD)["attempts"])
                    self.assertEqual(2, len(commands))
                    for command in commands:
                        self.assertEqual(["synthetic-codex", "queue", "--thread", THREAD, "--message"], command[:5])
                        self.assertNotIn("--model", command)
                    self.assertEqual("original-model", backend.inspect(THREAD).model)

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
                # Codex 0.161 uses underscore field names; quoted values must
                # still recover the original model and conversation.
                body = failure_body(
                    thread=f'"{THREAD}"', turn=f'"{TURN}"', model='"original-model"',
                    thread_key="thread_id", turn_key="turn_id",
                )
                writer.execute("INSERT INTO logs VALUES (1,1000,0,?,?,?)", ("codex_core::session::turn", body, THREAD))
            writer.close()
            events, _ = LogSource(path).read_after(0)
            self.assertEqual(1, len(events))
            self.assertEqual((THREAD, TURN, "original-model"),
                             (events[0].thread_id, events[0].turn_id, events[0].model))
            # Translate only the test address after exercising the strict reader.
            from dataclasses import replace
            event = replace(events[0], thread_id="thread-1", turn_id="turn-failed")
            engine.observe(event)
            self.assertEqual("original-model", store.recovery(event.thread_id)["model"])
            engine.tick()
            self.assertEqual([], desktop.sent)
            now[0] += 30
            engine.tick()
            self.assertEqual(1, len(desktop.sent))
            sent = desktop.sent[0]["params"]["turnStart"]
            self.assertEqual(event.thread_id, sent["request"]["threadId"])
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

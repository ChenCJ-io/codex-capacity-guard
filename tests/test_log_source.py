"""Synthetic log fixtures; no test opens the user's Codex database."""

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from capacity_guard.log_source import FailureEvent, LogSource, SourceUnavailable


THREAD = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"
OTHER_TURN = "33333333-3333-4333-8333-333333333333"
CAPACITY = "Selected model is at capacity. Please try a different model."
TARGET = "codex_core::session::turn"


def failure_body(error=CAPACITY, *, turn=TURN, model="gpt-6-astra"):
    model_field = f" model={model}" if model is not None else ""
    return (
        f"thread{{thread.id={THREAD}}}:turn{{turn.id={turn}{model_field} "
        f"codex.turn.reasoning_effort=max}}:session_task.run:run_turn: "
        f"Turn error: {error}"
    )


class LogSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "logs_2.sqlite"
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute(
            "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, "
            "ts_nanos INTEGER, level TEXT, target TEXT, "
            "feedback_log_body TEXT, thread_id TEXT, process_uuid TEXT)"
        )
        self.writer.commit()
        self.source = LogSource(self.path)

    def insert(self, body=None, *, target=TARGET, thread=THREAD,
               ts=100, nanos=125_000_000):
        result = self.writer.execute(
            "INSERT INTO logs (ts, ts_nanos, level, target, "
            "feedback_log_body, thread_id) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, nanos, "ERROR", target,
             failure_body() if body is None else body, thread),
        )
        self.writer.commit()
        return result.lastrowid

    def test_capacity_error_has_only_routing_metadata(self):
        log_id = self.insert()
        events, cursor = self.source.read_after(0)
        self.assertEqual(cursor, log_id)
        self.assertEqual(events, [FailureEvent(
            log_id, 100.125, THREAD, TURN, "gpt-6-astra", True
        )])
        self.assertNotIn(CAPACITY, repr(events))

    def test_quoted_tool_and_user_text_do_not_trigger_recovery(self):
        self.insert(f"ToolCall: shell({failure_body()!r})",
                    target="codex_core::stream_events_utils")
        self.insert(failure_body(), target="codex_core::stream_events_utils")
        self.insert(CAPACITY)
        self.insert(f"The user said: {failure_body()}")
        last = self.insert(f"Turn error: {CAPACITY}")
        self.assertEqual(self.source.read_after(0), ([], last))

    def test_noncapacity_turn_error_is_returned_without_error_text(self):
        secret_error = "A synthetic credential-like value must remain private."
        log_id = self.insert(failure_body(secret_error))
        events, cursor = self.source.read_after(0)
        self.assertEqual(cursor, log_id)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].capacity)
        self.assertNotIn(secret_error, repr(events[0]))

    def test_capacity_sentence_must_be_the_complete_error(self):
        self.insert(failure_body(f"Server echoed: {CAPACITY}"))
        self.insert(failure_body(f"{CAPACITY} Additional error."))
        events, _ = self.source.read_after(0)
        self.assertEqual([event.capacity for event in events], [False, False])

    def test_model_missing_or_ambiguous_is_empty(self):
        self.insert(failure_body(model=None))
        self.insert(failure_body(model="gpt-6-astra model=gpt-6-other"))
        self.insert(failure_body(model='gpt-6-astra model="invalid"'))
        events, _ = self.source.read_after(0)
        self.assertEqual([event.model for event in events], ["", "", ""])

    def test_span_depth_does_not_need_a_fixed_shape(self):
        self.insert(
            f"turn{{id=local thread.id={THREAD} turn.id={TURN} "
            "model=gpt-6-astra}:child{count=1}:session_task.run:run_turn: "
            f"Turn error: {CAPACITY}"
        )
        events, _ = self.source.read_after(0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].turn_id, TURN)

    def test_missing_or_invalid_routing_fields_are_skipped(self):
        self.insert(thread=None)
        self.insert(thread="not-a-uuid")
        self.insert(thread=OTHER_TURN)
        self.insert(failure_body(turn="not-a-uuid"))
        self.insert(failure_body(turn=TURN + "malformed"))
        self.insert(failure_body().replace(f"turn.id={TURN}", "local.id=1"))
        self.insert(failure_body().replace(
            f"turn.id={TURN}", f"turn.id={TURN} turn.id={OTHER_TURN}"
        ))
        self.insert(failure_body().replace(
            f"turn.id={TURN}", f"turn.id={TURN} turn.id=malformed"
        ))
        self.insert(failure_body().replace(
            f"thread.id={THREAD}", "thread.id=malformed"
        ))
        last = self.insert(failure_body().replace("Turn error: ", "User text: "))
        self.assertEqual(self.source.read_after(0), ([], last))

    def test_pagination_advances_across_unrelated_logs_without_skipping(self):
        first = self.insert()
        self.insert("irrelevant", target="other")
        self.insert("irrelevant", target="other")
        self.insert("irrelevant", target="other")
        last = self.insert(failure_body(turn=OTHER_TURN))
        events, cursor = self.source.read_after(0, limit=2)
        self.assertEqual([event.log_id for event in events], [first])
        self.assertEqual(cursor, 2)
        self.assertEqual(self.source.read_after(cursor, limit=2), ([], 4))
        events, cursor = self.source.read_after(4, limit=2)
        self.assertEqual([event.log_id for event in events], [last])
        self.assertEqual(cursor, last)
        self.assertEqual(self.source.read_after(cursor, limit=2), ([], last))

    def test_gaps_in_ids_and_later_inserts_are_handled(self):
        self.insert()
        removed = self.insert()
        last = self.insert()
        self.writer.execute("DELETE FROM logs WHERE id = ?", (removed,))
        self.writer.commit()
        events, cursor = self.source.read_after(0, limit=2)
        self.assertEqual([event.log_id for event in events], [1, last])
        self.assertEqual(cursor, last)
        fresh = self.insert(failure_body(turn=OTHER_TURN))
        events, cursor = self.source.read_after(cursor, limit=2)
        self.assertEqual([event.log_id for event in events], [fresh])
        self.assertEqual(cursor, fresh)

    def test_watermark_initialization_ignores_historical_errors(self):
        self.assertEqual(self.source.high_watermark(), 0)
        old = self.insert()
        initial = self.source.high_watermark()
        self.assertEqual(initial, old)
        self.assertEqual(self.source.read_after(initial), ([], initial))
        fresh = self.insert(failure_body(turn=OTHER_TURN))
        events, cursor = self.source.read_after(initial)
        self.assertEqual([event.log_id for event in events], [fresh])
        self.assertEqual(cursor, fresh)

    def test_nanoseconds_are_added_to_seconds(self):
        self.insert(ts=1, nanos=123_456_789)
        self.insert(ts=1, nanos=999_999_999)
        self.insert(ts=1, nanos=-1)
        self.insert(ts=1, nanos=1_000_000_000)
        self.insert(ts=None)
        self.insert(nanos=None)
        events, _ = self.source.read_after(0)
        self.assertEqual(len(events), 2)
        self.assertAlmostEqual(events[0].timestamp, 1.123456789, places=9)
        self.assertAlmostEqual(events[1].timestamp, 1.999999999, places=9)

    def test_missing_database_is_not_created(self):
        path = Path(self.temp.name) / "missing.sqlite"
        source = LogSource(path)
        for operation in (source.high_watermark, lambda: source.read_after(0)):
            with self.subTest(operation=operation):
                with self.assertRaises(SourceUnavailable):
                    operation()
                self.assertFalse(path.exists())

    def test_missing_schema_is_unavailable(self):
        self.writer.execute("DROP TABLE logs")
        self.writer.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY)")
        self.writer.commit()
        with self.assertRaises(SourceUnavailable):
            self.source.read_after(0)

    def test_locked_database_is_unavailable_then_recovers(self):
        self.insert()
        self.writer.execute("BEGIN EXCLUSIVE")
        with self.assertRaises(SourceUnavailable):
            self.source.high_watermark()
        self.writer.rollback()
        self.assertEqual(self.source.high_watermark(), 1)

    def test_special_characters_in_database_path(self):
        path = Path(self.temp.name) / "logs #question?.sqlite"
        self.writer.commit()
        with closing(sqlite3.connect(path)) as destination:
            self.writer.backup(destination)
        self.assertEqual(LogSource(path).high_watermark(), 0)

    def test_invalid_arguments_fail_before_database_access(self):
        source = LogSource(Path(self.temp.name) / "missing.sqlite")
        for cursor, limit in ((-1, 1), (True, 1), (0, 0), (0, -1), (0, 1.5)):
            with self.subTest(cursor=cursor, limit=limit):
                with self.assertRaises(ValueError):
                    source.read_after(cursor, limit)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from capacity_guard.config import Settings
from capacity_guard.log_source import FailureEvent
from capacity_guard.recovery import RecoveryEngine, Snapshot
from capacity_guard.state import Store


THREAD = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"


class FakeBackend:
    def __init__(self):
        self.snapshot = Snapshot(TURN, "failed", "original-model")
        self.sent = []
        self.error = None

    def inspect(self, thread_id):
        return self.snapshot

    def resume(self, thread_id, message, model):
        self.sent.append((thread_id, message, model))
        if self.error:
            raise self.error
        return "new-turn"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.set("enabled", True)
        self.backend = FakeBackend()
        self.now = 1000.0
        self.engine = RecoveryEngine(self.store, self.backend, Settings(jitter=0), lambda: self.now, lambda: .5)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, turn=TURN, capacity=True, model="original-model", timestamp=None):
        return FailureEvent(1, timestamp or self.now, THREAD, turn, model, capacity)

    def due(self):
        self.engine.observe(self.event())
        self.now += 30

    def test_capacity_waits_then_continues_original_model_once(self):
        self.engine.observe(self.event())
        self.engine.tick()
        self.assertEqual([], self.backend.sent)
        self.now += 30
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(1, len(self.backend.sent))
        self.assertEqual("original-model", self.backend.sent[0][2])
        self.assertEqual(1, self.store.recovery(THREAD)["attempts"])

    def test_repeated_capacity_has_no_retry_count_limit(self):
        for attempt in range(30):
            turn = TURN if attempt == 0 else f"turn-{attempt}"
            self.backend.snapshot = Snapshot(turn, "failed", "original-model")
            self.engine.observe(self.event(turn=turn))
            state = self.store.recovery(THREAD)
            expected_delay = min(30 * 2 ** min(attempt, 10), 120)
            self.assertEqual(expected_delay, state["next_at"] - self.now)
            self.now = state["next_at"]
            self.engine.tick()
        self.assertEqual(30, len(self.backend.sent))

    def test_human_continuation_wins(self):
        self.due()
        self.backend.snapshot = Snapshot("human-turn", "inProgress", "original-model")
        self.engine.tick()
        self.assertFalse(self.backend.sent)
        self.assertEqual("cancelled", self.store.recovery(THREAD)["status"])

    def test_active_turn_and_user_draft_are_not_interrupted(self):
        for snapshot in [Snapshot(TURN, "inProgress"), Snapshot(TURN, "failed", has_draft=True)]:
            self.due()
            self.backend.snapshot = snapshot
            self.engine.tick()
            self.assertFalse(self.backend.sent)

    def test_changed_model_cancels_instead_of_switching_it_back(self):
        self.due()
        self.backend.snapshot = Snapshot(TURN, "failed", "user-chosen-model")
        self.engine.tick()
        self.assertFalse(self.backend.sent)
        self.assertEqual("model_changed", self.store.recovery(THREAD)["reason"])

    def test_non_capacity_error_is_not_retried(self):
        self.due()
        self.engine.observe(self.event(turn="other-turn", capacity=False))
        self.engine.tick()
        self.assertFalse(self.backend.sent)

    def test_global_disable_cancels_submission(self):
        self.due()
        self.store.set("enabled", False)
        self.engine.tick()
        self.assertFalse(self.backend.sent)

    def test_thread_scope(self):
        self.store.set("threads", ["another-thread"])
        self.engine.observe(self.event())
        self.assertIsNone(self.store.recovery(THREAD))

    def test_shrinking_scope_cancels_an_already_scheduled_retry(self):
        self.due()
        self.store.set("threads", ["another-thread"])
        self.engine.tick()
        self.assertFalse(self.backend.sent)
        self.assertEqual("scope_changed", self.store.recovery(THREAD)["reason"])

    def test_scope_change_while_inspecting_prevents_submission(self):
        self.due()
        def inspect(_):
            self.store.set("threads", ["another-thread"])
            return Snapshot(TURN, "failed")
        self.backend.inspect = inspect
        self.engine.tick()
        self.assertFalse(self.backend.sent)

    def test_old_failure_cannot_revive_cancelled_thread(self):
        self.store.cancel(THREAD, "interrupt", self.now)
        self.engine.observe(self.event(timestamp=self.now - 1))
        self.assertIsNone(self.store.recovery(THREAD))

    def test_unknown_model_never_uses_default_model(self):
        self.engine.observe(self.event(model=""))
        self.assertIsNone(self.store.recovery(THREAD))

    def test_timeout_is_reconciled_without_duplicate_submission(self):
        self.due()
        self.backend.error = TimeoutError()
        self.engine.tick()
        self.now += 120
        self.engine.tick()
        self.assertEqual(1, len(self.backend.sent))
        self.assertEqual("uncertain", self.store.recovery(THREAD)["status"])
        token = self.store.recovery(THREAD)["token"]
        self.backend.snapshot = Snapshot("new-turn", "completed", recovery_token=token)
        self.now += 2
        self.engine.tick()
        self.assertEqual("recovered", self.store.recovery(THREAD)["status"])

    def test_definite_non_delivery_can_be_retried(self):
        class Unavailable(Exception):
            outcome_unknown = False
        self.due()
        self.backend.error = Unavailable()
        self.engine.tick()
        self.backend.error = None
        self.now += 120
        self.engine.tick()
        self.assertEqual(2, len(self.backend.sent))

    def test_cancellation_during_inspection_wins(self):
        self.due()
        def inspect(_):
            self.store.cancel(THREAD, "interrupt", self.now)
            return Snapshot(TURN, "failed")
        self.backend.inspect = inspect
        self.engine.tick()
        self.assertFalse(self.backend.sent)

    def test_attempt_delay_is_bounded_even_after_many_failures(self):
        self.assertLessEqual(Settings().delay(10**6, 1), 138)


if __name__ == "__main__":
    unittest.main()

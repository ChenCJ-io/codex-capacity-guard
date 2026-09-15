"""Recovery policy, separate from the Codex transport and wall clock."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from .config import Settings
from .state import Store


MESSAGE_PREFIX = "[codex-capacity-guard recovery="


def recovery_message(token: str) -> str:
    return (
        f"{MESSAGE_PREFIX}{token}]\n"
        "Continue the task interrupted by temporary model capacity limits, using the same model. "
        "First check which actions have already completed and whether any commands are still "
        "running, then continue the remaining authorized work. Preserve the user's latest "
        "instructions and existing approval requirements."
    )


@dataclass(frozen=True)
class Snapshot:
    turn_id: str
    status: str
    model: str = ""
    recovery_token: str | None = None
    eligible: bool = True
    has_draft: bool = False


class Backend(Protocol):
    def inspect(self, thread_id: str) -> Snapshot: ...
    def resume(self, thread_id: str, message: str, model: str) -> str | None: ...


class RecoveryEngine:
    def __init__(self, store: Store, backend: Backend, settings: Settings, clock=time.time, rand=random.random):
        self.store, self.backend, self.settings = store, backend, settings
        self.clock, self.rand = clock, rand

    def observe(self, event) -> None:
        with self.store.transaction():
            if not self.store.eligible(event.thread_id, event.timestamp):
                return
            if not event.capacity:
                self.store.cancel(event.thread_id, "different_error", event.timestamp)
                return
            if not event.model:
                self.store.audit("missing_model", event.thread_id)
                return
            previous = self.store.recovery(event.thread_id)
            if previous and previous["turn_id"] == event.turn_id:
                return
            attempts = previous["attempts"] if previous and previous["status"] in {"submitted", "uncertain"} and previous["model"] == event.model else 0
            self.store.schedule(event, self.clock() + self.settings.delay(attempts, self.rand()))

    def tick(self) -> None:
        if not self.store.get("enabled", False):
            return
        for pending in self.store.pending(self.clock()):
            if not self.store.get("enabled", False):
                break
            self._recover(pending)

    def _recover(self, pending: dict) -> None:
        thread_id = pending["thread_id"]
        allowed = self.store.get("threads", [])
        if allowed and thread_id not in allowed:
            self.store.cancel(thread_id, "scope_changed")
            return
        try:
            snapshot = self.backend.inspect(thread_id)
        except Exception as error:
            # Transport exceptions must not carry prompts or raw upstream messages.
            self.store.update(thread_id, next_at=self.clock() + self.settings.max_delay, reason=type(error).__name__)
            return

        # Hooks can cancel this recovery while the socket read is in flight.
        # Never let a stale snapshot overwrite that cancellation.
        current = self.store.recovery(thread_id)
        if not self.store.get("enabled", False) or not current or current["status"] != pending["status"] or current["turn_id"] != pending["turn_id"] or current["token"] != pending["token"]:
            return

        if not snapshot.eligible:
            self.store.cancel(thread_id, "unsupported_thread")
            return
        if snapshot.has_draft:
            self.store.update(thread_id, next_at=self.clock() + self.settings.poll_interval, reason="user_draft")
            return
        if snapshot.model and snapshot.model != pending["model"]:
            self.store.cancel(thread_id, "model_changed")
            return

        if snapshot.turn_id != pending["turn_id"]:
            if pending["token"] and snapshot.recovery_token == pending["token"]:
                # Our continuation was accepted. A new log error, if any, schedules
                # the next attempt; completion or human intervention ends this one.
                if snapshot.status in {"completed", "interrupted"}:
                    self.store.update(thread_id, status="recovered" if snapshot.status == "completed" else "cancelled", reason=snapshot.status)
                else:
                    self.store.update(thread_id, status="submitted", submitted_turn=snapshot.turn_id, next_at=self.clock() + self.settings.poll_interval, reason="")
            else:
                self.store.cancel(thread_id, "conversation_advanced")
            return

        if snapshot.status != "failed":
            if snapshot.status in {"completed", "interrupted"}:
                self.store.cancel(thread_id, "conversation_advanced")
            else:
                self.store.update(thread_id, next_at=self.clock() + self.settings.poll_interval, reason="turn_not_failed")
            return

        if pending["status"] in {"uncertain", "submitted"}:
            # A timed-out write can still reach the desktop. Never blindly submit
            # a duplicate. Continue inspecting until its outcome is observable.
            self.store.update(thread_id, next_at=self.clock() + self.settings.poll_interval, reason="awaiting_delivery_confirmation")
            return

        token = str(uuid4())
        with self.store.transaction():
            current = self.store.recovery(thread_id)
            allowed = self.store.get("threads", [])
            if (allowed and thread_id not in allowed) or not self.store.get("enabled", False) or not current or current["status"] != "waiting" or current["turn_id"] != pending["turn_id"]:
                return
            self.store.update(thread_id, status="sending", token=token, attempts=current["attempts"] + 1, reason="")

        try:
            # Hook cancellation is checked immediately before the external write.
            current = self.store.recovery(thread_id)
            allowed = self.store.get("threads", [])
            if (allowed and thread_id not in allowed) or not self.store.get("enabled", False) or current["status"] != "sending" or current["token"] != token:
                return
            result_turn = self.backend.resume(thread_id, recovery_message(token), pending["model"])
        except Exception as error:
            # A transport can explicitly prove no bytes were submitted. Everything
            # else is ambiguous and must be reconciled against the original thread.
            outcome_unknown = getattr(error, "outcome_unknown", True)
            with self.store.transaction():
                current = self.store.recovery(thread_id)
                if current and current["status"] == "sending" and current["token"] == token:
                    self.store.update(thread_id, status="uncertain" if outcome_unknown else "waiting", next_at=self.clock() + self.settings.max_delay, reason=type(error).__name__)
                    self.store.audit("delivery_uncertain" if outcome_unknown else "delivery_unavailable", thread_id)
            return

        with self.store.transaction():
            current = self.store.recovery(thread_id)
            if current and current["status"] == "sending" and current["token"] == token:
                self.store.update(thread_id, status="submitted", submitted_turn=result_turn, next_at=self.clock() + self.settings.poll_interval)
                self.store.audit("continuation_sent", thread_id)

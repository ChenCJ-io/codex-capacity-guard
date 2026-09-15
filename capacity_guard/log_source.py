"""Read bounded batches of genuine Codex turn failures from its SQLite log.

This source deliberately exposes only routing metadata, never the error text or
other log bodies. A new watcher should start at ``high_watermark()`` so that
installing it does not retry historical failures.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from typing import Iterator
from uuid import UUID


_TURN_TARGET = "codex_core::session::turn"
_ERROR_BOUNDARY = "}:session_task.run:run_turn: Turn error: "
_CAPACITY_ERROR = (
    "Selected model is at capacity. Please try a different model."
)
_REQUIRED_COLUMNS = {
    "id", "ts", "ts_nanos", "target", "feedback_log_body", "thread_id"
}
_UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UUID_RE = re.compile(_UUID_PATTERN)
_SPAN_START_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\{")
_SPAN_FIELD_RE = re.compile(
    r"(?:^|[\s{])(thread\.id|turn\.id|model)=([^\s{}]*)"
)
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}")


class SourceUnavailable(RuntimeError):
    """The log database cannot currently be read safely."""


@dataclass(frozen=True, slots=True)
class FailureEvent:
    log_id: int
    timestamp: float
    thread_id: str
    turn_id: str
    model: str
    capacity: bool


def _uuid(value: object) -> str | None:
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        return None
    return str(UUID(value))


def _parse_failure(row: tuple) -> FailureEvent | None:
    log_id, seconds, nanos, body, thread_value = row
    thread_id = _uuid(thread_value)
    if (
        thread_id is None
        or not isinstance(body, str)
        or not isinstance(seconds, int)
        or not isinstance(nanos, int)
        or not 0 <= nanos < 1_000_000_000
    ):
        return None

    prefix, boundary, error = body.partition(_ERROR_BOUNDARY)
    if (
        not boundary
        or not error.strip()
        or _SPAN_START_RE.match(prefix) is None
        or "\n" in prefix
        or "\r" in prefix
    ):
        return None

    # Span depth can vary between Codex versions. Accept a consistent UUID
    # wherever it occurs in the prefix, but never look in the error body.
    fields = _SPAN_FIELD_RE.findall(prefix)
    turn_ids = {_uuid(value) for name, value in fields if name == "turn.id"}
    if len(turn_ids) != 1 or None in turn_ids:
        return None
    span_thread_ids = {
        _uuid(value) for name, value in fields if name == "thread.id"
    }
    if span_thread_ids and span_thread_ids != {thread_id}:
        return None

    models = {value for name, value in fields if name == "model"}
    model = next(iter(models)) if len(models) == 1 else ""
    if _MODEL_RE.fullmatch(model) is None:
        model = ""
    return FailureEvent(
        log_id=log_id,
        timestamp=seconds + nanos / 1_000_000_000,
        thread_id=thread_id,
        turn_id=turn_ids.pop(),
        model=model,
        capacity=error.strip() == _CAPACITY_ERROR,
    )


class LogSource:
    """Incrementally read a ``logs_2.sqlite`` file without creating it.

    ``limit`` bounds the number of log IDs inspected, including unrelated rows.
    The returned cursor advances over those rows even when no event matches.
    Consumers must persist that cursor instead of deriving it from events.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=0.25)
            connection.execute("PRAGMA query_only = ON")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(logs)")
            }
            if not _REQUIRED_COLUMNS <= columns:
                raise SourceUnavailable("Codex log database schema is unavailable.")
            # Pin the watermark and the bounded page to the same read snapshot.
            connection.execute("BEGIN")
            yield connection
        except (sqlite3.Error, OSError):
            # Database exceptions must not leak the contents of log records.
            raise SourceUnavailable("Codex log database is unavailable.") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _watermark(connection: sqlite3.Connection) -> int:
        value = connection.execute("SELECT MAX(id) FROM logs").fetchone()[0]
        if value is None:
            return 0
        if not isinstance(value, int) or value < 0:
            raise SourceUnavailable("Codex log database has invalid record IDs.")
        return value

    def high_watermark(self) -> int:
        """Return the newest log ID; an empty database has watermark zero."""
        with self._connection() as connection:
            return self._watermark(connection)

    def read_after(
        self, cursor: int, limit: int = 1000
    ) -> tuple[list[FailureEvent], int]:
        """Read one bounded page strictly after ``cursor`` in ascending order."""
        if type(cursor) is not int or cursor < 0:
            raise ValueError("cursor must be a nonnegative integer")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")

        with self._connection() as connection:
            upper = self._watermark(connection)
            if cursor >= upper:
                return [], cursor

            # First bound the scan using only IDs. Fetch bodies only from the
            # runtime's turn-error target, not user or tool log messages.
            next_cursor = connection.execute(
                "SELECT MAX(id) FROM ("
                "SELECT id FROM logs WHERE id > ? AND id <= ? "
                "ORDER BY id LIMIT ?)",
                (cursor, upper, limit),
            ).fetchone()[0]
            if next_cursor is None:
                return [], upper
            rows = connection.execute(
                "SELECT id, ts, ts_nanos, feedback_log_body, thread_id "
                "FROM logs WHERE id > ? AND id <= ? AND target = ? "
                "AND instr(feedback_log_body, ?) > 0 ORDER BY id",
                (cursor, next_cursor, _TURN_TARGET, _ERROR_BOUNDARY),
            )
            events = []
            for row in rows:
                event = _parse_failure(row)
                if event is not None:
                    events.append(event)
            return events, next_cursor

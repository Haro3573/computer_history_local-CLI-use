"""Watermark and Memory index persistence for the Memory Pipeline.

Append-only, like `state_samples`, for the same reason:
`tests/test_store.py`'s `test_only_update_statement_in_package_touches_duration`
enforces that the only UPDATE anywhere in this package touches `duration`.
Advancing the `Watermark` therefore INSERTs a new row and reads back the
latest, rather than UPDATE-ing one tracked value -- `ADR-0005`'s "advances
only after a successful write" is naturally append-only shaped anyway: each
advance is its own durable fact, not a mutation of the last one.

Shares the same SQLite file `Store` already uses (`CONTEXT.md`'s `Memory
index` entry: "a SQLite table, separate from `state_samples`" -- separate
table, not a separate database).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .store import DEFAULT_STORE

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_watermark (
    id           INTEGER PRIMARY KEY,
    advanced_to  REAL NOT NULL,
    at           REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_index (
    id            INTEGER PRIMARY KEY,
    date          TEXT NOT NULL UNIQUE,
    path          TEXT NOT NULL,
    range_start   REAL NOT NULL,
    range_end     REAL NOT NULL,
    generated_at  REAL NOT NULL,
    content_hash  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_index_date ON memory_index(date);
"""

# The Collector runs concurrently as a long-lived launchd process writing to
# this same file (this project's normal deployment shape, not a hypothetical
# one), so a `summarize` run's writes need real tolerance for lock
# contention -- more than sqlite3's own 5s default.
_CONNECT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class MemoryIndexRow:
    """One `Daily memory` file's index entry -- `CONTEXT.md`'s `Memory
    index` shape: path, time range, generated-at, content hash."""

    date: str  # "YYYY-MM-DD", local calendar date
    path: str
    range_start: datetime
    range_end: datetime
    generated_at: datetime
    content_hash: str


def _memory_index_row_from_sqlite(row: sqlite3.Row) -> MemoryIndexRow:
    return MemoryIndexRow(
        date=row["date"],
        path=row["path"],
        range_start=datetime.fromtimestamp(row["range_start"], tz=UTC),
        range_end=datetime.fromtimestamp(row["range_end"], tz=UTC),
        generated_at=datetime.fromtimestamp(row["generated_at"], tz=UTC),
        content_hash=row["content_hash"],
    )


class PipelineStore:
    """SQLite-backed reader/writer for `pipeline_watermark` and `memory_index`."""

    def __init__(self, path: Path | str = DEFAULT_STORE) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), timeout=_CONNECT_TIMEOUT_SECONDS)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PipelineStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def watermark(self) -> datetime | None:
        row = self._connection.execute(
            "SELECT advanced_to FROM pipeline_watermark ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return datetime.fromtimestamp(row["advanced_to"], tz=UTC) if row else None

    def advance_watermark(self, to: datetime, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._connection.execute(
            "INSERT INTO pipeline_watermark (advanced_to, at) VALUES (?, ?)",
            (to.timestamp(), now.timestamp()),
        )
        self._connection.commit()

    def record_memory(self, row: MemoryIndexRow) -> None:
        """One row per date, never more. `OR REPLACE` (not a bare INSERT)
        because a crash between this write and `advance_watermark` leaves
        the Watermark untouched, so the next run legitimately reprocesses
        the same day (`ADR-0005`) -- without the `date` UNIQUE constraint
        and this replace, that retry would leave an orphaned duplicate row
        behind instead of superseding the first one. Still an INSERT, not an
        UPDATE: SQLite implements `OR REPLACE` as delete-then-insert, so
        `tests/test_store.py`'s package-wide "only UPDATE touches duration"
        scan stays satisfied."""
        self._connection.execute(
            "INSERT OR REPLACE INTO memory_index "
            "(date, path, range_start, range_end, generated_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                row.date,
                row.path,
                row.range_start.timestamp(),
                row.range_end.timestamp(),
                row.generated_at.timestamp(),
                row.content_hash,
            ),
        )
        self._connection.commit()

    def memories_for_date(self, date: str) -> list[MemoryIndexRow]:
        cursor = self._connection.execute(
            "SELECT * FROM memory_index WHERE date = ? ORDER BY id DESC",
            (date,),
        )
        return [_memory_index_row_from_sqlite(row) for row in cursor.fetchall()]

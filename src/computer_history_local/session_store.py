"""`Session cursor`, `Session slice`, and `sessions/*.md` index persistence
-- ticket #31's storage layer, mirroring `pipeline_store.py`'s shape for the
same reasons: append-only where possible (`tests/test_store.py`'s
`test_only_update_statement_in_package_touches_duration` enforces that the
only `UPDATE` anywhere in this package touches `duration`, so every write
here is `INSERT OR REPLACE`, never `UPDATE`), and sharing the same SQLite
file `Store`/`PipelineStore` already use rather than a separate database.

Three tables, one per `CONTEXT.md` concept:

- `session_cursor` -- one row per session file, the byte offset already
  read into slices (`Session cursor`).
- `session_slices` -- one row per (session file, local day), the already-
  reduced text for that file's turns on that day (`Session slice`).
- `session_memory_index` -- one row per local day, naming the rendered
  `sessions/YYYY-MM-DD.md` file -- same shape as `pipeline_store.py`'s
  `memory_index`, kept as a separate table rather than reused, since a
  day's `AI session` coverage and its `Daily memory` coverage are tracked
  and can legitimately diverge (a day can have one without the other).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .store import DEFAULT_STORE

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_cursor (
    id            INTEGER PRIMARY KEY,
    session_path  TEXT NOT NULL UNIQUE,
    tool          TEXT NOT NULL,
    byte_offset   INTEGER NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_slices (
    id            INTEGER PRIMARY KEY,
    session_path  TEXT NOT NULL,
    tool          TEXT NOT NULL,
    date          TEXT NOT NULL,
    start_at      REAL NOT NULL,
    end_at        REAL NOT NULL,
    raw_turns     TEXT NOT NULL,
    text          TEXT NOT NULL,
    UNIQUE(session_path, date)
);
CREATE INDEX IF NOT EXISTS session_slices_date ON session_slices(date);
CREATE TABLE IF NOT EXISTS session_memory_index (
    id            INTEGER PRIMARY KEY,
    date          TEXT NOT NULL UNIQUE,
    path          TEXT NOT NULL,
    generated_at  REAL NOT NULL,
    content_hash  TEXT NOT NULL
);
"""

# The Collector, and potentially a concurrently-running `process-sessions`,
# both touch this same file -- same tolerance `pipeline_store.py` uses, for
# the same lock-contention reason.
_CONNECT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class SessionCursor:
    session_path: str
    tool: str
    byte_offset: int
    updated_at: datetime


@dataclass(frozen=True)
class SessionSlice:
    """One `Session slice` -- CONTEXT.md: one session file's turns for one
    local day. `raw_turns` (JSON via `ai_sessions.turns_to_json`) is kept
    alongside the already-reduced `text` so a later incremental update can
    re-reduce the *whole* day's turns (old + newly appended), not just the
    new ones -- otherwise a slice re-processed across several
    `process-sessions` runs on a resumed session would silently drift past
    its budget, a turn's worth at a time. `start_at`/`end_at` are the
    earliest/latest turn timestamps, kept for ordering slices within a
    rendered day, not for display."""

    session_path: str
    tool: str
    date: str  # "YYYY-MM-DD", local calendar date
    start_at: datetime
    end_at: datetime
    raw_turns: str
    text: str


@dataclass(frozen=True)
class SessionMemoryIndexRow:
    date: str
    path: str
    generated_at: datetime
    content_hash: str


def _cursor_from_sqlite(row: sqlite3.Row) -> SessionCursor:
    return SessionCursor(
        session_path=row["session_path"],
        tool=row["tool"],
        byte_offset=row["byte_offset"],
        updated_at=datetime.fromtimestamp(row["updated_at"], tz=UTC),
    )


def _slice_from_sqlite(row: sqlite3.Row) -> SessionSlice:
    return SessionSlice(
        session_path=row["session_path"],
        tool=row["tool"],
        date=row["date"],
        start_at=datetime.fromtimestamp(row["start_at"], tz=UTC),
        end_at=datetime.fromtimestamp(row["end_at"], tz=UTC),
        raw_turns=row["raw_turns"],
        text=row["text"],
    )


def _index_row_from_sqlite(row: sqlite3.Row) -> SessionMemoryIndexRow:
    return SessionMemoryIndexRow(
        date=row["date"],
        path=row["path"],
        generated_at=datetime.fromtimestamp(row["generated_at"], tz=UTC),
        content_hash=row["content_hash"],
    )


class SessionStore:
    """SQLite-backed reader/writer for `session_cursor`, `session_slices`,
    and `session_memory_index`."""

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

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def cursor_for(self, session_path: str) -> SessionCursor | None:
        row = self._connection.execute(
            "SELECT * FROM session_cursor WHERE session_path = ?", (session_path,)
        ).fetchone()
        return _cursor_from_sqlite(row) if row else None

    def record_cursor(
        self, session_path: str, tool: str, byte_offset: int, *, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(UTC)
        self._connection.execute(
            "INSERT OR REPLACE INTO session_cursor (session_path, tool, byte_offset, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (session_path, tool, byte_offset, now.timestamp()),
        )
        self._connection.commit()

    def slice_for(self, session_path: str, date: str) -> SessionSlice | None:
        row = self._connection.execute(
            "SELECT * FROM session_slices WHERE session_path = ? AND date = ?",
            (session_path, date),
        ).fetchone()
        return _slice_from_sqlite(row) if row else None

    def record_slice(self, slice_: SessionSlice) -> None:
        """One row per (session_path, date), never more -- `OR REPLACE` so
        re-processing the same file's same day (new turns appended since
        last time) supersedes the old text rather than accumulating a
        second row, mirroring `pipeline_store.record_memory`'s exact
        reasoning."""
        self._connection.execute(
            "INSERT OR REPLACE INTO session_slices "
            "(session_path, tool, date, start_at, end_at, raw_turns, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                slice_.session_path,
                slice_.tool,
                slice_.date,
                slice_.start_at.timestamp(),
                slice_.end_at.timestamp(),
                slice_.raw_turns,
                slice_.text,
            ),
        )
        self._connection.commit()

    def slices_for_date(self, date: str) -> list[SessionSlice]:
        cursor = self._connection.execute(
            "SELECT * FROM session_slices WHERE date = ? ORDER BY start_at ASC",
            (date,),
        )
        return [_slice_from_sqlite(row) for row in cursor.fetchall()]

    def record_memory(self, row: SessionMemoryIndexRow) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO session_memory_index "
            "(date, path, generated_at, content_hash) VALUES (?, ?, ?, ?)",
            (row.date, row.path, row.generated_at.timestamp(), row.content_hash),
        )
        self._connection.commit()

    def memory_for_date(self, date: str) -> SessionMemoryIndexRow | None:
        row = self._connection.execute(
            "SELECT * FROM session_memory_index WHERE date = ?", (date,)
        ).fetchone()
        return _index_row_from_sqlite(row) if row else None

    def all_dates(self) -> list[str]:
        """Every distinct date with a rendered `sessions/*.md` -- mirrors
        `PipelineStore.all_dates`, same reason: `Retrieval`'s `ask` needs to
        gather every day that has something, not just days it's told to
        look at directly."""
        cursor = self._connection.execute(
            "SELECT DISTINCT date FROM session_memory_index ORDER BY date ASC"
        )
        return [row["date"] for row in cursor.fetchall()]

    def all_slice_dates(self) -> list[str]:
        """Every distinct date with at least one `Session slice`, whether or
        not that date has been rendered into `session_memory_index` yet --
        `process_sessions_once`'s self-healing pass over this list is what
        makes the two-write render step actually match `Session cursor`'s
        own claimed "only advance after success" shape (`ADR-0005`'s
        analogue) rather than merely asserting it in a docstring."""
        cursor = self._connection.execute("SELECT DISTINCT date FROM session_slices ORDER BY date ASC")
        return [row["date"] for row in cursor.fetchall()]

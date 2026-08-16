"""State samples, merged by pulsetime rather than deduplicated by state key.

ADR-0003: adjacent readings with identical data merge into one row by
extending `duration`, but only within a `Pulsetime` window — outside that
window, identical data starts a fresh row instead. Silence is never assumed
to be a continuation of the last known state.

This is the one deliberate exception to the append-only discipline
`adhd_lifelog`'s `activity_store.py` enforces strictly (no UPDATE, no
DELETE): here, `duration` on the newest row of a kind may be extended in
place. Nothing else is ever mutated -- `test_only_update_statement_in_package_touches_duration`
in `tests/test_store.py` enforces that scope, across the whole package, so
the exception can't quietly widen.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

KIND_WINDOW = "window"
KIND_IDLE = "idle"
# KIND_BROWSER arrives with its own ticket.

DEFAULT_STORE = Path("~/.local/share/computer-history-local/state.sqlite3")
DEFAULT_PULSETIME = timedelta(minutes=5)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_samples (
    id       INTEGER PRIMARY KEY,
    kind     TEXT    NOT NULL,
    at       REAL    NOT NULL,
    duration REAL    NOT NULL,
    app      TEXT,
    title    TEXT,
    url      TEXT,
    away     INTEGER
);
CREATE INDEX IF NOT EXISTS state_samples_kind_at ON state_samples(kind, at);
"""


def _key(app: str | None, title: str | None, url: str | None, away: bool | None) -> tuple:
    """What counts as "the same situation" for a merge decision.

    Shared by `Observation` and `Row` so the definition of sameness lives in
    exactly one place. `away` is part of the key deliberately: leaving a
    boolean like this out looks harmless and silently merges idle=True with
    idle=False into one situation that was true of neither (the exact bug
    `adhd_lifelog`'s `activity_store.py` `state_key` docstring warns about).
    """
    return (app, title, url, away)


@dataclass(frozen=True)
class Observation:
    """One kind-specific fact read from a `Sample`, not yet stored.

    Deliberately not named `Event`: CONTEXT.md's `State sample` entry says to
    avoid that word here, since an observation is a point-in-time reading, not
    the persisting `State sample` it may merge into.
    """

    kind: str
    at: datetime
    app: str | None = None
    title: str | None = None
    url: str | None = None
    away: bool | None = None

    def _data(self) -> tuple:
        return _key(self.app, self.title, self.url, self.away)


@dataclass(frozen=True)
class Row:
    """A `State sample` row as read back from storage."""

    id: int
    kind: str
    at: datetime
    duration: float
    app: str | None
    title: str | None
    url: str | None
    away: bool | None = None

    def _data(self) -> tuple:
        return _key(self.app, self.title, self.url, self.away)

    @property
    def confirmed_until(self) -> datetime:
        """The last moment this row's data was actually observed."""
        return self.at + timedelta(seconds=self.duration)


@dataclass(frozen=True)
class Extend:
    """Merge: the new observation repeats the last row within pulsetime."""

    row_id: int
    new_duration: float


@dataclass(frozen=True)
class Insert:
    """New data, or a repeat of old data after a real gap: write a fresh row."""

    observation: Observation


Action = Extend | Insert


def merge_or_insert(
    last_row: Row | None,
    observation: Observation,
    *,
    pulsetime: timedelta = DEFAULT_PULSETIME,
) -> Action:
    """Decide whether `observation` extends `last_row` or starts a new one.

    `last_row` must already be the last row of `observation`'s own kind --
    comparing across kinds silently defeats merging (`adhd_lifelog`'s
    `activity_store.py` `last_event` docstring: a window sample compared
    against the last idle row never matches, so nothing is ever merged;
    "2,400 checks produced 2,400 rows"). Passing a different-kind row is a
    caller bug, not a "no match" case, so it raises rather than silently
    falling through to Insert.
    """
    if last_row is not None and last_row.kind != observation.kind:
        raise ValueError(
            f"merge_or_insert requires a same-kind last_row: "
            f"got kind={last_row.kind!r} for observation kind={observation.kind!r}"
        )

    if last_row is not None and last_row._data() == observation._data():
        gap = observation.at - last_row.confirmed_until
        if gap <= pulsetime:
            new_duration = (observation.at - last_row.at).total_seconds()
            return Extend(row_id=last_row.id, new_duration=new_duration)

    return Insert(observation=observation)


def _row_from_sqlite(row: sqlite3.Row) -> Row:
    return Row(
        id=row["id"],
        kind=row["kind"],
        at=datetime.fromtimestamp(row["at"], tz=UTC),
        duration=row["duration"],
        app=row["app"],
        title=row["title"],
        url=row["url"],
        away=None if row["away"] is None else bool(row["away"]),
    )


class Store:
    """SQLite-backed reader/writer over one `state_samples` table."""

    def __init__(self, path: Path | str = DEFAULT_STORE) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._migrate()
        self._connection.commit()

    def _migrate(self) -> None:
        """Add columns a pre-existing database predates.

        `CREATE TABLE IF NOT EXISTS` no-ops against a table that already
        exists, so a database written before a column existed (e.g. `away`,
        added in ticket #3) would otherwise never get it and every INSERT
        naming that column would fail.
        """
        existing_columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(state_samples)")
        }
        if "away" not in existing_columns:
            self._connection.execute("ALTER TABLE state_samples ADD COLUMN away INTEGER")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def last_row(self, kind: str) -> Row | None:
        row = self._connection.execute(
            "SELECT * FROM state_samples WHERE kind = ? ORDER BY at DESC, id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return _row_from_sqlite(row) if row else None

    def record(
        self, observation: Observation, *, pulsetime: timedelta = DEFAULT_PULSETIME
    ) -> Action:
        """Apply `merge_or_insert`'s decision: UPDATE `duration` or INSERT a row."""
        last = self.last_row(observation.kind)
        action = merge_or_insert(last, observation, pulsetime=pulsetime)
        if isinstance(action, Extend):
            self._connection.execute(
                "UPDATE state_samples SET duration = ? WHERE id = ?",
                (action.new_duration, action.row_id),
            )
        else:
            self._connection.execute(
                "INSERT INTO state_samples (kind, at, duration, app, title, url, away) "
                "VALUES (?, ?, 0.0, ?, ?, ?, ?)",
                (
                    observation.kind,
                    observation.at.timestamp(),
                    observation.app,
                    observation.title,
                    observation.url,
                    None if observation.away is None else int(observation.away),
                ),
            )
        self._connection.commit()
        return action

    def rows(self, *, kind: str | None = None) -> list[Row]:
        if kind is None:
            cursor = self._connection.execute(
                "SELECT * FROM state_samples ORDER BY at ASC, id ASC"
            )
        else:
            cursor = self._connection.execute(
                "SELECT * FROM state_samples WHERE kind = ? ORDER BY at ASC, id ASC",
                (kind,),
            )
        return [_row_from_sqlite(row) for row in cursor.fetchall()]

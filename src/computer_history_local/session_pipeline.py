"""Wire `ai_sessions.py`'s discovery/parsing/reduction to `session_store.py`
-- ticket #30's `process-sessions` orchestration, mirroring
`memory_pipeline.py`'s shape (batch, reduce, write, advance) for the same
"kept thin, testable end-to-end with a fake source" reasoning.

Unlike `memory_pipeline.py`, there is no `Watermark`, no day ordering, and no
consent gate to respect in sequence: `ADR-0009` means every session file is
eligible to read the moment it's found, and files have no dependency on each
other the way days do under a single advancing marker. A file that errors
doesn't block the rest -- there's nothing here for a later file's processing
to silently skip past, the failure mode `_process_day`'s sequential stop
exists to prevent.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .ai_sessions import (
    DEFAULT_SESSION_ROOTS,
    Section,
    SessionFile,
    Turn,
    discover_session_files,
    is_live_session,
    read_new_turns,
    reduce_turns,
    render_day_markdown,
    turns_by_local_day,
    turns_from_json,
    turns_to_json,
)
from .session_store import SessionMemoryIndexRow, SessionSlice, SessionStore

DEFAULT_SESSIONS_DIR = Path("~/.local/share/computer-history-local/sessions")


@dataclass(frozen=True)
class ProcessSessionsResult:
    """What one `process-sessions` run did -- shaped for the CLI to report
    without knowing anything about `SessionFile`/`SessionSlice` internals."""

    files_processed: int
    files_skipped_live: int
    new_turn_count: int
    days_written: tuple[str, ...]
    errors: tuple[str, ...] = ()


def _merge_and_reduce(
    session_store: SessionStore, session_file: SessionFile, day: str, new_turns: list[Turn]
) -> SessionSlice:
    """Combine `new_turns` with whatever this (file, day) already has, and
    re-reduce over the *whole* set -- not just the new turns. Re-reducing
    only the new turns each run would let a slice's total size drift past
    `SLICE_CHAR_BUDGET` a little more on every `process-sessions` run
    against a resumed session; re-reducing the whole known set keeps the
    budget an actual cap, not a per-run suggestion."""
    existing = session_store.slice_for(str(session_file.path), day)
    all_turns = (turns_from_json(existing.raw_turns) if existing else []) + new_turns
    all_turns.sort(key=lambda t: t.at)
    return SessionSlice(
        session_path=str(session_file.path),
        tool=session_file.tool,
        date=day,
        start_at=all_turns[0].at,
        end_at=all_turns[-1].at,
        raw_turns=turns_to_json(all_turns),
        text=reduce_turns(all_turns),
    )


def _dates_needing_write(session_store: SessionStore, affected_days: set[str]) -> set[str]:
    """Every day this run must (re)render: the days it actually touched
    this run, plus any day that has `Session slice`s but no matching,
    still-existing `session_memory_index` entry -- an orphan left behind by
    a previous run that wrote slices, then failed (lock contention, a
    permissions error) before recording the index row or replacing the
    file. Without this second half, `Session cursor`'s "only advance after
    success" claim (CONTEXT.md) would be true of the cursor alone and false
    of the render step behind it -- an orphaned day would stay invisible to
    `retrieve`/`ask` forever, since nothing else ever revisits a day this
    run didn't itself add new turns to."""
    needing = set(affected_days)
    for day in session_store.all_slice_dates():
        row = session_store.memory_for_date(day)
        if row is None or not Path(row.path).exists():
            needing.add(day)
    return needing


def _write_day(session_store: SessionStore, day: str, sessions_dir: Path, *, now: datetime) -> None:
    slices = session_store.slices_for_date(day)
    if not slices:
        return
    sections = [
        Section(tool=s.tool, start_at=s.start_at, end_at=s.end_at, text=s.text) for s in slices
    ]
    markdown = render_day_markdown(day, sections)
    path = sessions_dir / f"{day}.md"
    tmp_path = path.with_suffix(".md.tmp")
    sessions_dir.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(markdown, encoding="utf-8")
    try:
        session_store.record_memory(
            SessionMemoryIndexRow(
                date=day,
                path=str(path),
                generated_at=now,
                content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            )
        )
    except sqlite3.OperationalError:
        # The Collector, and potentially another `process-sessions` run,
        # can hold this same file locked -- same lock-contention risk
        # `memory_pipeline._process_day` already treats as a real, expected
        # failure, not a hypothetical one. Leaves no index row and no
        # renamed file; `_dates_needing_write` picks this day back up on
        # the next run rather than silently losing it.
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(path)


def process_sessions_once(
    session_store: SessionStore,
    *,
    roots: tuple[tuple[Path, str], ...] = DEFAULT_SESSION_ROOTS,
    sessions_dir: Path = DEFAULT_SESSIONS_DIR,
    now: datetime | None = None,
) -> ProcessSessionsResult:
    """One `process-sessions` run: read every session file's new turns past
    its `Session cursor`, fold them into the affected days' `Session
    slice`s, render every touched day's `sessions/YYYY-MM-DD.md`, and
    advance each file's cursor.

    The `Live session` (CONTEXT.md) is skipped entirely, every run -- never
    read, never given a cursor, so once it closes a later run picks it up
    like any other file rather than resuming from a cursor that was never
    started.
    """
    now = now or datetime.now(UTC)
    sessions_dir = sessions_dir.expanduser()

    files_processed = 0
    files_skipped_live = 0
    new_turn_count = 0
    affected_days: set[str] = set()
    errors: list[str] = []

    for session_file in discover_session_files(roots):
        if is_live_session(session_file):
            files_skipped_live += 1
            continue

        try:
            cursor = session_store.cursor_for(str(session_file.path))
            from_offset = cursor.byte_offset if cursor is not None else 0
            result = read_new_turns(session_file, from_offset=from_offset)
        except OSError as exc:
            errors.append(f"{session_file.path}: {exc}")
            continue

        if not result.turns:
            if result.new_offset != from_offset:
                session_store.record_cursor(
                    str(session_file.path), session_file.tool, result.new_offset, now=now
                )
            continue

        files_processed += 1
        new_turn_count += len(result.turns)
        by_day = turns_by_local_day(result.turns)
        for day, day_turns in by_day.items():
            day_str = day.isoformat()
            slice_ = _merge_and_reduce(session_store, session_file, day_str, day_turns)
            session_store.record_slice(slice_)
            affected_days.add(day_str)

        session_store.record_cursor(str(session_file.path), session_file.tool, result.new_offset, now=now)

    written_days: set[str] = set()
    for day_str in sorted(_dates_needing_write(session_store, affected_days)):
        try:
            _write_day(session_store, day_str, sessions_dir, now=now)
        except (OSError, sqlite3.OperationalError) as exc:
            errors.append(f"{day_str}: {exc}")
        else:
            written_days.add(day_str)

    return ProcessSessionsResult(
        files_processed=files_processed,
        files_skipped_live=files_skipped_live,
        new_turn_count=new_turn_count,
        days_written=tuple(sorted(written_days)),
        errors=tuple(errors),
    )

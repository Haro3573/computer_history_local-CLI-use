"""Wire the day batcher to the provider and stores: batch, summarize, write,
advance.

Kept thin and separate from `pipeline_store.py` and `providers/` so a fake
provider can drive this loop end-to-end in tests, mirroring `collector.py`'s
own reasoning for the same split (`adhd_lifelog`'s `claude-cli` provider
passed every unit test and still failed at the entry point).

Days are always local-calendar days (mirrors `adhd_lifelog`'s own
`.astimezone()` convention for `day`, `reentry.py`, and `auto_run.py`'s daily
cap) -- a person's day doesn't align with UTC. `Today` is never summarized:
it's still accumulating, so it can't yet be a complete `Daily memory`
(`CONTEXT.md`).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from .pipeline_store import MemoryIndexRow, PipelineStore
from .providers.claude_cli import DaySummaryState, TimelineEntry
from .store import KIND_BROWSER, KIND_IDLE, KIND_WINDOW, Row, Store

DEFAULT_MEMORY_DIR = Path("~/.local/share/computer-history-local/memories")


def pending_days(
    *, watermark: datetime | None, earliest: datetime | None, now: datetime
) -> list[date]:
    """Local calendar dates that need summarizing.

    From the day after the `Watermark`'s covered day (or, with no watermark
    yet, from the earliest captured sample's day) through yesterday --
    never today.
    """
    if earliest is None:
        return []

    today_local = now.astimezone().date()
    if watermark is not None:
        start_day = watermark.astimezone().date() + timedelta(days=1)
    else:
        start_day = earliest.astimezone().date()

    days: list[date] = []
    day = start_day
    while day < today_local:
        days.append(day)
        day += timedelta(days=1)
    return days


def _local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of `day` in local time, each endpoint computed fresh via
    `.astimezone()` on its own naive midnight rather than by adding
    `timedelta(days=1)` to an already-localized datetime. The latter reuses
    that datetime's fixed UTC offset, which goes stale across a DST
    transition -- reproduced directly: for 2026-03-08 (US spring-forward),
    `start_local + timedelta(days=1)` lands on a UTC instant one hour off
    from the *real* local midnight of 2026-03-09, and the watermark built
    from it silently reads back as the wrong day."""
    start_local = datetime.combine(day, time.min).astimezone()
    end_local = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    return start_local, end_local


def rows_for_day(rows: list[Row], day: date) -> list[Row]:
    """Rows whose [at, confirmed_until) span overlaps `day`'s local window.

    Not `row.at`'s date alone: a session left open across midnight merges
    (via `Store`'s pulsetime extend) into one row whose `at` is still
    yesterday but whose `confirmed_until` reaches into today. Filtering by
    `at`'s date alone attributes that whole row to the day it started and
    leaves the day it continues into with zero rows for it -- reproduced
    directly with a Terminal window open 23:00-01:58. A row that spans a
    boundary is included in both adjacent days on purpose; the day it isn't
    relevant to just won't contain it.
    """
    start_local, end_local = _local_day_bounds(day)
    return [
        row
        for row in rows
        if row.at.astimezone() < end_local and row.confirmed_until.astimezone() > start_local
    ]


def format_day_for_prompt(rows: list[Row]) -> str:
    """Deterministic, compact text rendering of a day's rows -- what
    actually gets sent to the provider. Ticket #15 wraps this with a
    content cap and splits further when it's exceeded; on its own this is
    a full day's input."""
    lines = []
    for row in sorted(rows, key=lambda r: r.at):
        start_local = row.at.astimezone()
        end_local = row.confirmed_until.astimezone()
        span = f"{start_local:%H:%M}–{end_local:%H:%M}"
        minutes = row.duration / 60
        if row.kind == KIND_WINDOW:
            detail = f"{row.app or '?'} — {row.title or ''}".rstrip(" —")
        elif row.kind == KIND_IDLE:
            detail = "away" if row.away else "active"
        elif row.kind == KIND_BROWSER:
            detail = row.url or ""
        else:
            detail = ""
        lines.append(f"{span} ({minutes:.0f}m) {row.kind}: {detail}")
    return "\n".join(lines)


def render_daily_memory(day: date, entries: tuple[TimelineEntry, ...]) -> str:
    """Structured timeline entries -> the actual `Daily memory` Markdown
    (`CONTEXT.md`: "time range -> summary -> contributing apps", not
    free-form prose)."""
    lines = [f"# {day.isoformat()}", ""]
    for entry in entries:
        lines.append(f"## {entry.time_range}")
        lines.append("")
        lines.append(entry.summary)
        if entry.apps:
            lines.append("")
            lines.append(f"Apps: {', '.join(entry.apps)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class SummarizeOutcome:
    day: date
    written: bool
    error: str | None = None


def summarize_once(
    store: Store,
    pipeline_store: PipelineStore,
    provider,
    *,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
    now: datetime | None = None,
) -> list[SummarizeOutcome]:
    """One `summarize --send` run.

    Finds pending days, makes one provider call per day (`ADR-0007`), writes
    the `Daily memory` file + `memory_index` row, and advances the
    `Watermark` only once that day's write succeeds (`ADR-0005`). Days
    process in chronological order and processing stops at the first
    failure -- a later day can never be marked done while an earlier one
    silently failed, which would break the Watermark's meaning of
    "everything before this point is summarized."
    """
    now = now or datetime.now(UTC)
    all_rows = store.rows()
    earliest = min((row.at for row in all_rows), default=None)
    watermark = pipeline_store.watermark()
    days = pending_days(watermark=watermark, earliest=earliest, now=now)

    outcomes: list[SummarizeOutcome] = []
    memory_dir = memory_dir.expanduser()

    for day in days:
        day_rows = rows_for_day(all_rows, day)

        if not day_rows:
            # Nothing captured -- no reason to spend a paid provider call
            # summarizing an empty day. Nothing can retroactively appear for
            # a past day (the Collector never backfills), so it's safe to
            # advance past it rather than leaving it pending forever.
            start_local, end_local = _local_day_bounds(day)
            try:
                pipeline_store.advance_watermark(end_local - timedelta(microseconds=1), now=now)
            except sqlite3.OperationalError as exc:
                outcomes.append(SummarizeOutcome(day=day, written=False, error=str(exc)))
                break
            outcomes.append(SummarizeOutcome(day=day, written=False))
            continue

        day_text = format_day_for_prompt(day_rows)
        result = provider.summarize(day, day_text)

        if result.state != DaySummaryState.COMPLETED:
            outcomes.append(SummarizeOutcome(day=day, written=False, error=result.error_message))
            break

        markdown = render_daily_memory(day, result.entries)
        start_local, end_local = _local_day_bounds(day)
        path = memory_dir / f"{day.isoformat()}.md"
        try:
            memory_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            pipeline_store.record_memory(
                MemoryIndexRow(
                    date=day.isoformat(),
                    path=str(path),
                    range_start=start_local,
                    range_end=end_local,
                    generated_at=now,
                    content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                )
            )
            # The last instant of `day`, not the start of the next one --
            # so `watermark().date()` reads as "the last day that's
            # covered," and `pending_days`'s `+ timedelta(days=1)` starts
            # exactly one day later.
            pipeline_store.advance_watermark(end_local - timedelta(microseconds=1), now=now)
        except (sqlite3.OperationalError, OSError) as exc:
            # The Collector runs concurrently as a long-lived launchd
            # process writing to the same SQLite file (this project's own
            # normal deployment shape) -- lock contention is a real,
            # expected failure mode here, not a hypothetical one, and must
            # degrade to a per-day failure rather than crash `summarize`
            # outright and lose the outcomes already collected this run.
            outcomes.append(SummarizeOutcome(day=day, written=False, error=str(exc)))
            break

        outcomes.append(SummarizeOutcome(day=day, written=True))

    return outcomes

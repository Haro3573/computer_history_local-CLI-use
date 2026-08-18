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

# A deliberately conservative starting number, not derived from any hard
# model/context limit (Sonnet's window is far larger) -- ADR-0007's whole
# point is keeping one unit of work small for a future small/local model,
# not maximizing what fits in one call. Tunable; nothing else depends on
# this exact value.
DEFAULT_CONTENT_CAP_CHARS = 4000

# Below this, a window stops splitting even if still over cap -- a single
# row's own formatted line can't be shrunk further, and this floor (twice
# the Collector's own poll interval) keeps recursion from chasing
# millisecond-scale windows on a pathological input.
MIN_CHUNK_WINDOW = timedelta(minutes=1)

# A day needing more chunks than this fails outright instead of silently
# making that many real, Pro-subscription-backed provider calls. Without a
# cap, a window whose title changes every poll (a live counter, a download
# percentage) defeats Store's Pulsetime merge and produces one row roughly
# every 30 seconds all day -- MIN_CHUNK_WINDOW alone lets that recurse to
# 1,000+ leaf chunks with no warning. 24 is generous (roughly one chunk per
# hour) for any day that isn't already degenerate in this way.
MAX_CHUNKS_PER_DAY = 24


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


def _assignment_time(row: Row, window_start: datetime) -> datetime:
    """The timestamp used to bucket a row into a chunk: the row's own local
    start, or the window's start if the row began before it. A row that
    started the previous day and merged past midnight (`rows_for_day`
    includes it via interval overlap, not a same-day `at`) would otherwise
    fall outside every sub-window of *this* day and silently vanish from
    every chunk instead of landing in the first one."""
    local_at = row.at.astimezone()
    return local_at if local_at > window_start else window_start


@dataclass(frozen=True)
class Chunk:
    start: datetime
    end: datetime
    rows: list[Row]
    text: str  # `format_day_for_prompt(rows)`, computed once and reused --
    # not recomputed by the caller for the same rows.


def day_chunks(
    rows: list[Row], day: date, *, cap_chars: int = DEFAULT_CONTENT_CAP_CHARS
) -> list[Chunk]:
    """Split `day`'s rows into `Chunk`s, each formatting to at most
    `cap_chars` -- recursively halving the local-time window when a chunk
    is still over cap. Unlike `rows_for_day`'s deliberate duplication
    across adjacent days, a row here is assigned to exactly one chunk
    (`_assignment_time`): the same day's rows feed one output file, so
    counting a row twice would summarize it twice.

    Only non-empty chunks are returned -- ticket #14 already established
    "no data, no provider call" for a whole day; the same principle applies
    to a sub-day chunk with nothing in it.
    """
    day_start, day_end = _local_day_bounds(day)
    return [
        chunk
        for chunk in _split_window(rows, day_start, day_end, day_start=day_start, cap_chars=cap_chars)
        if chunk.rows
    ]


def _split_window(
    rows: list[Row],
    start: datetime,
    end: datetime,
    *,
    day_start: datetime,
    cap_chars: int,
) -> list[Chunk]:
    # `rows` is already this node's own subset -- each recursive call below
    # passes its half, not the full day, so re-filtering at depth N scans
    # only that branch's rows, not the whole day's every time.
    window_rows = [row for row in rows if start <= _assignment_time(row, day_start) < end]
    text = format_day_for_prompt(window_rows)
    if len(text) <= cap_chars or (end - start) <= MIN_CHUNK_WINDOW or not window_rows:
        return [Chunk(start=start, end=end, rows=window_rows, text=text)]
    mid = start + (end - start) / 2
    return _split_window(
        window_rows, start, mid, day_start=day_start, cap_chars=cap_chars
    ) + _split_window(window_rows, mid, end, day_start=day_start, cap_chars=cap_chars)


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
    # Set only when `written` is False and `error` is None -- distinguishes
    # *why* nothing happened (no data vs. already covered) so the CLI isn't
    # stuck reporting "no data captured" for a day that in fact had plenty,
    # just already summarized.
    skipped_reason: str | None = None


def _advance_watermark_past(
    day: date, pipeline_store: PipelineStore, *, now: datetime
) -> SummarizeOutcome | None:
    """Advance past `day` with no provider call (nothing to summarize, or
    it's already covered). Returns a failure outcome on lock contention,
    `None` on success -- the caller appends `SummarizeOutcome(written=False)`
    itself so both call sites read the same way."""
    start_local, end_local = _local_day_bounds(day)
    try:
        pipeline_store.advance_watermark(end_local - timedelta(microseconds=1), now=now)
    except sqlite3.OperationalError as exc:
        return SummarizeOutcome(day=day, written=False, error=str(exc))
    return None


def _process_day(
    day: date,
    day_rows: list[Row],
    pipeline_store: PipelineStore,
    provider,
    *,
    memory_dir: Path,
    content_cap_chars: int,
    now: datetime,
    advance_watermark: bool,
) -> SummarizeOutcome:
    """Chunk `day_rows`, call the provider once per chunk (`ADR-0007`), and
    write the combined `Daily memory` file + `memory_index` row -- a day is
    summarized completely or not at all, so any chunk failure fails the
    whole day and nothing partial is written.

    `advance_watermark` is `False` for an explicit `--reprocess`
    (ticket #16's AC: an override must never move the `Watermark`) and
    `True` for the normal pending-days loop (`ADR-0005`: only after a
    successful write).
    """
    if not day_rows:
        if advance_watermark:
            # Nothing can retroactively appear for a past day -- the
            # Collector never backfills -- so it's safe to advance past an
            # empty day with no write at all, rather than leaving it
            # pending forever. (The one intentional case where the
            # Watermark moves without a write; ADR-0005's title is about
            # never moving *without a successful outcome*, and "correctly
            # identified as empty" is one.)
            failure = _advance_watermark_past(day, pipeline_store, now=now)
            if failure is not None:
                return failure
        return SummarizeOutcome(day=day, written=False, skipped_reason="no data captured")

    chunks = day_chunks(day_rows, day, cap_chars=content_cap_chars)

    if len(chunks) > MAX_CHUNKS_PER_DAY:
        return SummarizeOutcome(
            day=day,
            written=False,
            error=(
                f"{len(chunks)} chunks needed (max {MAX_CHUNKS_PER_DAY}) -- "
                "this day's data is unusually dense; check the Collector for "
                "a runaway state (e.g. a title that changes every poll)."
            ),
        )

    entries: list[TimelineEntry] = []
    for chunk in chunks:
        result = provider.summarize(
            day, chunk.text, window_start=chunk.start, window_end=chunk.end
        )
        if result.state != DaySummaryState.COMPLETED:
            return SummarizeOutcome(day=day, written=False, error=result.error_message)
        entries.extend(result.entries)

    markdown = render_daily_memory(day, tuple(entries))
    start_local, end_local = _local_day_bounds(day)
    path = memory_dir / f"{day.isoformat()}.md"
    # Written to a temp path and only `replace()`d into `path` (an atomic
    # rename on the same filesystem) after the DB write succeeds -- not
    # written to `path` directly first. `record_memory` (`OR REPLACE`) is
    # what lets `--reprocess` overwrite an existing row rather than
    # accumulate a second one, but that also means a DB failure *after* an
    # in-place file write would leave `path` holding new content while
    # `memory_index` still names the old content_hash, and nothing
    # revisits an already-covered day to notice. Lock contention with the
    # concurrently-running Collector (this project's real deployment
    # shape) is the realistic failure here, not a local rename -- ordering
    # the DB write first means that far-more-likely failure leaves `path`
    # untouched instead of silently wrong.
    tmp_path = path.with_suffix(".md.tmp")
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(markdown, encoding="utf-8")
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
        if advance_watermark:
            # The last instant of `day`, not the start of the next one --
            # so `watermark().date()` reads as "the last day that's
            # covered," and `pending_days`'s `+ timedelta(days=1)` starts
            # exactly one day later.
            pipeline_store.advance_watermark(end_local - timedelta(microseconds=1), now=now)
        tmp_path.replace(path)
    except (sqlite3.OperationalError, OSError) as exc:
        # The Collector runs concurrently as a long-lived launchd process
        # writing to the same SQLite file (this project's own normal
        # deployment shape) -- lock contention is a real, expected failure
        # mode here, not a hypothetical one, and must degrade to a per-day
        # failure rather than crash `summarize` outright.
        tmp_path.unlink(missing_ok=True)
        return SummarizeOutcome(day=day, written=False, error=str(exc))

    return SummarizeOutcome(day=day, written=True)


def summarize_once(
    store: Store,
    pipeline_store: PipelineStore,
    provider,
    *,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
    content_cap_chars: int = DEFAULT_CONTENT_CAP_CHARS,
    reprocess: date | None = None,
    now: datetime | None = None,
) -> list[SummarizeOutcome]:
    """One `summarize --send` run.

    Finds pending days; for each, splits its rows into one or more
    `cap_chars`-bounded chunks (`day_chunks`) and makes one provider call
    per chunk (`ADR-0007`) -- a day within the cap is exactly one chunk, one
    call, no regression from the core ticket. Writes the file +
    `memory_index` row and advances the `Watermark` only once that day's
    write succeeds (`ADR-0005`). Days process in chronological order and
    processing stops at the first failure -- a later day can never be
    marked done while an earlier one silently failed, which would break the
    Watermark's meaning of "everything before this point is summarized."

    A day already covered by an existing `Daily memory` (checked directly
    against `memory_index`, not just inferred from `Watermark` position --
    a day can be recorded there without the `Watermark` yet reflecting it,
    e.g. a crash between the two writes) is skipped before any provider
    call, same as an empty day: no cost, no error, `Watermark` still
    advances past it (ticket #16's AC).

    `reprocess`, when given, ignores pending-days entirely and force-
    reprocesses exactly that one day -- calls the provider and overwrites
    its file+index even if already covered, and never touches the
    `Watermark` either way (ticket #16's AC: an override must not move it
    or change which days a later plain run considers already-covered).
    """
    now = now or datetime.now(UTC)
    memory_dir = memory_dir.expanduser()

    if reprocess is not None:
        if reprocess >= now.astimezone().date():
            return [
                SummarizeOutcome(
                    day=reprocess,
                    written=False,
                    error="cannot reprocess today or a future date -- it isn't complete yet",
                )
            ]
        day_rows = rows_for_day(store.rows(), reprocess)
        return [
            _process_day(
                reprocess,
                day_rows,
                pipeline_store,
                provider,
                memory_dir=memory_dir,
                content_cap_chars=content_cap_chars,
                now=now,
                advance_watermark=False,
            )
        ]

    all_rows = store.rows()
    earliest = min((row.at for row in all_rows), default=None)
    watermark = pipeline_store.watermark()
    days = pending_days(watermark=watermark, earliest=earliest, now=now)

    outcomes: list[SummarizeOutcome] = []

    for day in days:
        try:
            already_covered = bool(pipeline_store.memories_for_date(day.isoformat()))
        except sqlite3.OperationalError as exc:
            # Same lock-contention risk as every other store call in this
            # loop -- a SELECT is not exempt just because it doesn't write.
            outcomes.append(SummarizeOutcome(day=day, written=False, error=str(exc)))
            break

        if already_covered:
            failure = _advance_watermark_past(day, pipeline_store, now=now)
            if failure is not None:
                outcomes.append(failure)
                break
            outcomes.append(
                SummarizeOutcome(day=day, written=False, skipped_reason="already covered")
            )
            continue

        day_rows = rows_for_day(all_rows, day)
        outcome = _process_day(
            day,
            day_rows,
            pipeline_store,
            provider,
            memory_dir=memory_dir,
            content_cap_chars=content_cap_chars,
            now=now,
            advance_watermark=True,
        )
        outcomes.append(outcome)
        if outcome.error is not None:
            break

    return outcomes

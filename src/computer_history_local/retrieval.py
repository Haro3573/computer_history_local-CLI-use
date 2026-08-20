"""Look up or ask questions about `Daily memory` files -- Retrieval, the
third sub-system `CONTEXT.md`'s opening line promises ("retrievable
later").

Two independent modes, tickets #28/#29:

- `lookup`: free, deterministic. Reads `memory_index` + the files directly,
  never a provider call.
- `gather_all_daily_memories` / `AskPreview` / `ask_once`: `ask`'s free half
  and its paid half. `ask_once` is the orchestration `summarize_once`
  already has in `memory_pipeline.py` -- call the provider, check its
  state, shape the result -- kept here rather than inline in `__main__.py`,
  which otherwise has to import a provider's own result-state enum just to
  branch on it.

Kept separate from `memory_pipeline.py`: that module batches, summarizes,
writes, and advances the `Watermark` from raw `State sample`s. This module
never writes anything -- it only ever reads what `Memory Pipeline` already
produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .ai_sessions import (
    DEFAULT_SESSION_ROOTS,
    PARSERS,
    Section,
    discover_session_files,
    is_live_session,
    reduce_turns,
    render_day_markdown,
)
from .pipeline_store import PipelineStore
from .providers.claude_cli import DaySummaryState
from .session_store import SessionStore


class MemoryFileMissingError(RuntimeError):
    """A `memory_index`/`session_memory_index` row names a file that no
    longer exists on disk -- e.g. deleted out from under the index after
    being recorded. Raised loudly here rather than left as a bare
    `FileNotFoundError` surfacing far from the row/file mismatch that
    actually caused it."""


def _read_memory_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MemoryFileMissingError(
            f"memory_index names {path}, but that file no longer exists"
        ) from exc


def _read_session_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MemoryFileMissingError(
            f"session_memory_index names {path}, but that file no longer exists"
        ) from exc


def has_live_session(roots: tuple[tuple[Path, str], ...] = DEFAULT_SESSION_ROOTS) -> bool:
    """Whether a `Live session` (CONTEXT.md) exists right now -- a cheap,
    read-no-file-content check (just directory listing + filename/env-var
    comparison), used to decide whether the "current session omitted"
    notice applies at all before doing any real work."""
    return any(is_live_session(f) for f in discover_session_files(roots))


def _live_today_sections(
    now: datetime, roots: tuple[tuple[Path, str], ...] = DEFAULT_SESSION_ROOTS
) -> list[Section]:
    """Today's `AI session` content, read fresh every call -- never cached,
    never written anywhere (`ADR-0009`'s Q4 grilling answer: recent, not-
    yet-`process-sessions`-ed data reads live, no copy). A file whose mtime
    doesn't fall on today can't hold anything from today either -- session
    files are only ever appended to, never backdated -- so this never opens
    a file that provably can't contribute, which is what keeps a live scan
    affordable against a corpus that can run into the hundreds of
    megabytes (measured: ~1.1GB / 567 files across this project's own
    Claude Code + Codex history)."""
    today = now.astimezone().date()
    sections: list[Section] = []
    for session_file in discover_session_files(roots):
        if is_live_session(session_file):
            continue
        try:
            mtime = datetime.fromtimestamp(session_file.path.stat().st_mtime, tz=UTC)
            if mtime.astimezone().date() != today:
                continue
            data = session_file.path.read_bytes()
        except OSError:
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        turns = sorted(
            (t for t in PARSERS[session_file.tool](lines) if t.at.astimezone().date() == today),
            key=lambda t: t.at,
        )
        if not turns:
            continue
        sections.append(
            Section(
                tool=session_file.tool, start_at=turns[0].at, end_at=turns[-1].at, text=reduce_turns(turns)
            )
        )
    return sections


def _session_text_for_date(
    session_store: SessionStore | None,
    day: date,
    *,
    now: datetime,
    roots: tuple[tuple[Path, str], ...] = DEFAULT_SESSION_ROOTS,
) -> str | None:
    """`AI session` text for `day`: today always reads live (never cached,
    per `_live_today_sections`'s own docstring); any other day reads
    whatever `process-sessions` has already cached, or nothing at all --
    there is no live fallback for a past day, since finding one would mean
    scanning the entire session corpus rather than just today's handful of
    actively-written files (the exact cost `_live_today_sections`'s mtime
    pre-filter exists to avoid)."""
    if session_store is None:
        return None
    if day == now.astimezone().date():
        sections = _live_today_sections(now, roots)
        return render_day_markdown(day.isoformat(), sections) if sections else None
    row = session_store.memory_for_date(day.isoformat())
    return _read_session_file(row.path) if row is not None else None


def _combine(daily_text: str | None, session_text: str | None) -> str | None:
    """Two distinct sections, never one blended narrative -- `CONTEXT.md`'s
    `Retrieval` entry: the correction of `adhd_lifelog`'s own documented
    mistake of merging activity data and session evidence into one
    undifferentiated prompt."""
    parts = [text for text in (daily_text, f"## AI sessions\n\n{session_text}" if session_text else None) if text]
    return "\n\n".join(parts) if parts else None


@dataclass(frozen=True)
class LookupResult:
    """`dates` is every day that had a `Daily memory` file and/or `AI
    session` content; `missing` is every requested day that had neither.
    Both matter: a range spanning a not-yet-`summarize`d day (e.g. one that
    reaches into yesterday) should show what's there rather than erroring
    the whole range, but must say what it's missing rather than silently
    dropping it (`_find_overlap`'s own reasoning: silent failure is worse
    than a loud one). `live_session_excluded` is true only when today falls
    within the requested range and a `Live session` was actually found and
    skipped -- ticket #31's Q11: singular, and only when it's actually
    true, never a standing notice."""

    dates: tuple[date, ...]
    missing: tuple[date, ...]
    text: str  # concatenated content of found days, in date order
    live_session_excluded: bool = False


def lookup(
    pipeline_store: PipelineStore,
    start: date,
    end: date | None = None,
    *,
    session_store: SessionStore | None = None,
    now: datetime | None = None,
    session_roots: tuple[tuple[Path, str], ...] = DEFAULT_SESSION_ROOTS,
) -> LookupResult:
    """Ticket #28: `end` inclusive; a single day if omitted. A single day
    that's missing surfaces as `missing=(that day,)` with empty `text` --
    the CLI layer decides whether that's an error (ticket #28: a single
    missing day just errors) or a partial range (shown with what's there).

    `session_store` is optional and additive (ticket #31): omitted, this
    behaves exactly as it always has -- `Daily memory` only. Passed, each
    day's text gains a separate `## AI sessions` section when there's
    anything to add (`_combine`), and a day with session content but no
    `Daily memory` yet (today, always; a past day `process-sessions` has
    covered but `summarize` hasn't) counts as found rather than missing.
    """
    now = now or datetime.now(UTC)
    end = end or start
    if end < start:
        start, end = end, start

    found_dates: list[date] = []
    found_texts: list[str] = []
    missing_dates: list[date] = []

    day = start
    while day <= end:
        rows = pipeline_store.memories_for_date(day.isoformat())
        daily_text = _read_memory_file(rows[0].path) if rows else None
        session_text = _session_text_for_date(session_store, day, now=now, roots=session_roots)
        combined = _combine(daily_text, session_text)
        if combined is not None:
            found_texts.append(combined)
            found_dates.append(day)
        else:
            missing_dates.append(day)
        day += timedelta(days=1)

    today = now.astimezone().date()
    live_session_excluded = (
        session_store is not None and start <= today <= end and has_live_session(session_roots)
    )

    return LookupResult(
        dates=tuple(found_dates),
        missing=tuple(missing_dates),
        text="\n\n".join(found_texts),
        live_session_excluded=live_session_excluded,
    )


@dataclass(frozen=True)
class AskPreview:
    """What a real `ask --send` would send, at zero cost -- mirrors
    `summarize`'s own free/no-`--send` preview (ticket #17)."""

    dates: tuple[date, ...]
    total_chars: int
    live_session_excluded: bool = False


def gather_all_daily_memories(pipeline_store: PipelineStore) -> list[tuple[date, str]]:
    """Every existing `Daily memory` file, oldest first, as `(date, raw
    text)`. Ticket #29's v1 retrieval mechanism: `ask` sends all of these,
    no pre-filtering -- see the map's own corpus-size measurement for why
    that's fine at this project's actual scale."""
    pairs = []
    for date_str in pipeline_store.all_dates():
        rows = pipeline_store.memories_for_date(date_str)
        if not rows:
            continue
        pairs.append((date.fromisoformat(date_str), _read_memory_file(rows[0].path)))
    pairs.sort(key=lambda pair: pair[0])
    return pairs


def gather_all_context(
    pipeline_store: PipelineStore,
    session_store: SessionStore | None = None,
    *,
    now: datetime | None = None,
    session_roots: tuple[tuple[Path, str], ...] = DEFAULT_SESSION_ROOTS,
) -> list[tuple[date, str]]:
    """`gather_all_daily_memories`, plus (ticket #31, optional and additive
    like `lookup`'s own `session_store` param) every day's `AI session`
    content folded in as its own section (`_combine`) -- including a day
    that has session content but no `Daily memory` at all, which today
    always is (`Daily memory` never covers today; `AI session` always can,
    read live)."""
    now = now or datetime.now(UTC)
    daily = dict(gather_all_daily_memories(pipeline_store))
    session_texts: dict[date, str] = {}

    if session_store is not None:
        today = now.astimezone().date()
        for date_str in session_store.all_dates():
            day = date.fromisoformat(date_str)
            if day == today:
                continue  # today is handled once, live, below -- never from cache
            text = _session_text_for_date(session_store, day, now=now, roots=session_roots)
            if text:
                session_texts[day] = text
        today_text = _session_text_for_date(session_store, today, now=now, roots=session_roots)
        if today_text:
            session_texts[today] = today_text

    all_days = set(daily) | set(session_texts)
    pairs = []
    for day in sorted(all_days):
        combined = _combine(daily.get(day), session_texts.get(day))
        if combined is not None:
            pairs.append((day, combined))
    return pairs


def ask_preview(
    pipeline_store: PipelineStore,
    session_store: SessionStore | None = None,
    *,
    now: datetime | None = None,
    session_roots: tuple[tuple[Path, str], ...] = DEFAULT_SESSION_ROOTS,
) -> AskPreview:
    now = now or datetime.now(UTC)
    pairs = gather_all_context(pipeline_store, session_store, now=now, session_roots=session_roots)
    return AskPreview(
        dates=tuple(day for day, _text in pairs),
        total_chars=sum(len(text) for _day, text in pairs),
        live_session_excluded=session_store is not None and has_live_session(session_roots),
    )


@dataclass(frozen=True)
class AskResult:
    """`ask --send`'s outcome -- mirrors `SummarizeOutcome`'s shape
    (`memory_pipeline.py`): one field distinguishing *why* nothing was
    answered (no `Daily memory` yet vs. a provider failure) so `__main__.py`
    can print the right message without knowing anything about providers."""

    answered: bool
    answer: str = ""
    cited_dates: tuple[str, ...] = ()
    error: str | None = None
    empty_corpus: bool = False
    live_session_excluded: bool = False


def ask_once(
    pipeline_store: PipelineStore,
    provider,
    question: str,
    *,
    session_store: SessionStore | None = None,
    now: datetime | None = None,
    session_roots: tuple[tuple[Path, str], ...] = DEFAULT_SESSION_ROOTS,
) -> AskResult:
    """One `ask --send` call: gather every `Daily memory` (plus, when
    `session_store` is given, every day's `AI session` content -- ticket
    #30), ask `provider`, and shape whatever it returns -- the same
    orchestration `summarize_once` already does for `summarize --send`,
    given the same home this module's own docstring already claimed for
    it."""
    now = now or datetime.now(UTC)
    day_contents = gather_all_context(pipeline_store, session_store, now=now, session_roots=session_roots)
    live_session_excluded = session_store is not None and has_live_session(session_roots)
    if not day_contents:
        return AskResult(answered=False, empty_corpus=True, live_session_excluded=live_session_excluded)

    today = now.astimezone().date()
    result = provider.answer(question, day_contents, today=today)
    if result.state != DaySummaryState.COMPLETED:
        return AskResult(
            answered=False, error=result.error_message, live_session_excluded=live_session_excluded
        )
    return AskResult(
        answered=True,
        answer=result.answer,
        cited_dates=result.cited_dates,
        live_session_excluded=live_session_excluded,
    )

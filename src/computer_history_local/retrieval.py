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

from .pipeline_store import PipelineStore
from .providers.claude_cli import DaySummaryState


class MemoryFileMissingError(RuntimeError):
    """A `memory_index` row names a Daily memory file that no longer exists
    on disk -- e.g. deleted out from under the index after being recorded.
    Raised loudly here rather than left as a bare `FileNotFoundError`
    surfacing far from the row/file mismatch that actually caused it."""


def _read_memory_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MemoryFileMissingError(
            f"memory_index names {path}, but that file no longer exists"
        ) from exc


@dataclass(frozen=True)
class LookupResult:
    """`dates` is every day that had a `Daily memory` file; `missing` is
    every requested day that didn't. Both matter: a range spanning a
    not-yet-`summarize`d day (e.g. one that reaches into yesterday) should
    show what's there rather than erroring the whole range, but must say
    what it's missing rather than silently dropping it (`_find_overlap`'s
    own reasoning: silent failure is worse than a loud one)."""

    dates: tuple[date, ...]
    missing: tuple[date, ...]
    text: str  # concatenated content of found days, in date order


def lookup(pipeline_store: PipelineStore, start: date, end: date | None = None) -> LookupResult:
    """Ticket #28: `end` inclusive; a single day if omitted. A single day
    that's missing surfaces as `missing=(that day,)` with empty `text` --
    the CLI layer decides whether that's an error (ticket #28: a single
    missing day just errors) or a partial range (shown with what's there)."""
    end = end or start
    if end < start:
        start, end = end, start

    found_dates: list[date] = []
    found_texts: list[str] = []
    missing_dates: list[date] = []

    day = start
    while day <= end:
        rows = pipeline_store.memories_for_date(day.isoformat())
        if rows:
            found_texts.append(_read_memory_file(rows[0].path))
            found_dates.append(day)
        else:
            missing_dates.append(day)
        day += timedelta(days=1)

    return LookupResult(
        dates=tuple(found_dates), missing=tuple(missing_dates), text="\n\n".join(found_texts)
    )


@dataclass(frozen=True)
class AskPreview:
    """What a real `ask --send` would send, at zero cost -- mirrors
    `summarize`'s own free/no-`--send` preview (ticket #17)."""

    dates: tuple[date, ...]
    total_chars: int


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


def ask_preview(pipeline_store: PipelineStore) -> AskPreview:
    pairs = gather_all_daily_memories(pipeline_store)
    return AskPreview(
        dates=tuple(day for day, _text in pairs),
        total_chars=sum(len(text) for _day, text in pairs),
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


def ask_once(
    pipeline_store: PipelineStore, provider, question: str, *, now: datetime | None = None
) -> AskResult:
    """One `ask --send` call: gather every `Daily memory`, ask `provider`,
    and shape whatever it returns -- the same orchestration `summarize_once`
    already does for `summarize --send`, given the same home this module's
    own docstring already claimed for it."""
    now = now or datetime.now(UTC)
    day_contents = gather_all_daily_memories(pipeline_store)
    if not day_contents:
        return AskResult(answered=False, empty_corpus=True)

    today = now.astimezone().date()
    result = provider.answer(question, day_contents, today=today)
    if result.state != DaySummaryState.COMPLETED:
        return AskResult(answered=False, error=result.error_message)
    return AskResult(answered=True, answer=result.answer, cited_dates=result.cited_dates)

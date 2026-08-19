"""Look up or ask questions about `Daily memory` files -- Retrieval, the
third sub-system `CONTEXT.md`'s opening line promises ("retrievable
later").

Two independent modes, tickets #28/#29:

- `lookup`: free, deterministic. Reads `memory_index` + the files directly,
  never a provider call.
- `gather_all_daily_memories` / `AskPreview`: the free half of `ask`. The
  actual paid call lives on `ClaudeCliProvider.answer()`
  (`providers/claude_cli.py`) -- kept there, not here, the same split
  `memory_pipeline.py` already has between its own batching logic and the
  provider that actually spends a call.

Kept separate from `memory_pipeline.py`: that module batches, summarizes,
writes, and advances the `Watermark` from raw `State sample`s. This module
never writes anything -- it only ever reads what `Memory Pipeline` already
produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .pipeline_store import PipelineStore


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
            path = Path(rows[0].path)
            found_texts.append(path.read_text(encoding="utf-8"))
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
        text = Path(rows[0].path).read_text(encoding="utf-8")
        pairs.append((date.fromisoformat(date_str), text))
    pairs.sort(key=lambda pair: pair[0])
    return pairs


def ask_preview(pipeline_store: PipelineStore) -> AskPreview:
    pairs = gather_all_daily_memories(pipeline_store)
    return AskPreview(
        dates=tuple(day for day, _text in pairs),
        total_chars=sum(len(text) for _day, text in pairs),
    )

"""A provider that answers without a network, for wiring checks.

Mirrors `adhd_lifelog`'s `--provider fake`: proves the day-batch -> provider
-> file -> index -> watermark path runs end to end without spending a
subscription call or sending a word. Fixed answers, counts calls -- the whole
contract.
"""

from __future__ import annotations

from datetime import date, datetime

from .claude_cli import DaySummaryResult, DaySummaryState

FAKE_PROVIDER_ID = "fake"


class FakeProvider:
    """Deterministic, network-free, and obviously fake when it reaches a screen."""

    provider_id = FAKE_PROVIDER_ID

    def __init__(self) -> None:
        self.call_count = 0
        self.calls: list[tuple[date, list[str]]] = []

    def summarize(
        self, day: date, span_texts: list[str], *, window_start: datetime, window_end: datetime
    ) -> DaySummaryResult:
        self.call_count += 1
        self.calls.append((day, span_texts))
        window = f"{window_start.astimezone():%H:%M}–{window_end.astimezone():%H:%M}"
        summaries = tuple(
            f"(fake provider -- model not called; span {index + 1}/{len(span_texts)} "
            f"for {day.isoformat()} {window})"
            for index in range(len(span_texts))
        )
        return DaySummaryResult(state=DaySummaryState.COMPLETED, summaries=summaries)

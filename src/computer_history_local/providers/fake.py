"""A provider that answers without a network, for wiring checks.

Mirrors `adhd_lifelog`'s `--provider fake`: proves the day-batch -> provider
-> file -> index -> watermark path runs end to end without spending a
subscription call or sending a word. Fixed answer, counts calls -- the whole
contract.
"""

from __future__ import annotations

from datetime import date

from .claude_cli import DaySummaryResult, DaySummaryState, TimelineEntry

FAKE_PROVIDER_ID = "fake"


class FakeProvider:
    """Deterministic, network-free, and obviously fake when it reaches a screen."""

    provider_id = FAKE_PROVIDER_ID

    def __init__(self) -> None:
        self.call_count = 0
        self.calls: list[tuple[date, str]] = []

    def summarize(self, day: date, day_text: str) -> DaySummaryResult:
        self.call_count += 1
        self.calls.append((day, day_text))
        lines = day_text.count("\n") + 1 if day_text else 0
        return DaySummaryResult(
            state=DaySummaryState.COMPLETED,
            entries=(
                TimelineEntry(
                    time_range="00:00–23:59",
                    summary=(
                        f"(fake provider -- model not called; received "
                        f"{lines} line(s) of input for {day.isoformat()})"
                    ),
                    apps=(),
                ),
            ),
        )

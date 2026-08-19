"""A provider that answers without a network, for wiring checks.

Mirrors `adhd_lifelog`'s `--provider fake`: proves the day-batch -> provider
-> file -> index -> watermark path runs end to end without spending a
subscription call or sending a word. Fixed answers, counts calls -- the whole
contract.
"""

from __future__ import annotations

from datetime import date, datetime

from .claude_cli import AnswerResult, DaySummaryResult, DaySummaryState

FAKE_PROVIDER_ID = "fake"


class FakeProvider:
    """Deterministic, network-free, and obviously fake when it reaches a screen."""

    provider_id = FAKE_PROVIDER_ID

    def __init__(self) -> None:
        self.call_count = 0
        self.calls: list[tuple[date, list[str]]] = []
        self.answer_calls: list[tuple[str, int]] = []

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

    def answer(
        self, question: str, day_contents: list[tuple[date, str]], *, today: date
    ) -> AnswerResult:
        self.call_count += 1
        self.answer_calls.append((question, len(day_contents)))
        cited = tuple(day.isoformat() for day, _text in day_contents)
        return AnswerResult(
            state=DaySummaryState.COMPLETED,
            answer=(
                f"(fake provider -- model not called; received {len(day_contents)} "
                f"day(s) as of {today.isoformat()} for question: {question!r})"
            ),
            cited_dates=cited,
        )

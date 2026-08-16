"""Mask credentials before a window title or URL is ever written to storage.

Vendored from `adhd_lifelog`'s `redaction.py` rather than imported as a
runtime dependency (the two repos are deliberately independent -- see
CONTEXT.md's "Why a separate repo"). Applied by the collector before a
`window`-kind event is constructed: a terminal's title is its command line,
and the store it goes into makes masking after the fact too late by exactly
the amount that matters.
"""

from __future__ import annotations

import re


class DeterministicRedactor:
    """Minimum secret masking before a State sample is written."""

    _RULES = (
        ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")),
        ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}")),
        ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        (
            "generic_secret_assignment",
            re.compile(
                r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]{8,}"
            ),
        ),
    )

    def redact(self, text: str) -> tuple[str, tuple[str, ...]]:
        result = text
        applied: list[str] = []
        for name, pattern in self._RULES:
            result, count = pattern.subn(f"[REDACTED:{name}]", result)
            if count:
                applied.append(name)
        return result, tuple(applied)

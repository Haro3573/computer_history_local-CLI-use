"""The real local-LLM `Session processor` adapter -- ticket #32. Ollama's
HTTP API against `qwen3.5:4b`, `think: false`, one call per day producing
one prose paragraph.

Not the interactive `ollama run` CLI: benchmarked against real session data
on this project's own dev machine, `/no_think` typed into `ollama run` did
not suppress the model's chain-of-thought (still 80-136s per call and still
reasoning visibly). `think: false` on the HTTP API actually works --
16s for a real day's assistant turns, content verified accurate against the
source. `urllib.request` (stdlib), not `requests`: this project has no HTTP
dependency yet and Ollama's API is plain JSON-over-HTTP, not worth adding
one for.

Turn-level chunking (`Personal_Inliner`'s own map-reduce, evaluated for
reuse) was deliberately not built here: that project needed it because its
model couldn't reliably handle a whole window in one call. `qwen3.5:4b`
handled a full day's assistant turns in one call in the benchmark that
picked it. Reintroduce chunking if a future, weaker model actually needs
it -- not preemptively.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .ai_sessions import ROLE_ASSISTANT, Turn

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:4b"

# Derived from one real benchmark (this project's own dev machine, M3,
# 8GB RAM): ~3,969 chars of pre-compressed prompt took ~16s with
# think:false -- roughly 250 chars/second. A single fixed timeout was
# rejected during grilling (a busy day's transcript dwarfs a quiet one's);
# this scales instead. Both constants are starting points from one data
# point, not settled across enough real days to be considered tuned.
BASE_TIMEOUT_SECONDS = 120.0
CHARS_PER_SECOND = 250.0

_PROMPT_TEMPLATE = (
    "Summarize what this person worked on in one short paragraph (2-4 "
    "sentences), based only on the AI assistant's own turns below from one "
    "day's session. Be concrete about what was built, fixed, or decided -- "
    "never invent a file path, command, number, or detail that isn't "
    "actually present in the text below.\n\n{turns}\n"
)


class OllamaUnavailableError(RuntimeError):
    """Ollama isn't reachable, or the configured model isn't pulled.
    Raised by `check_available`, called once at the start of a
    `process-sessions` run (`session_pipeline.py`) so the whole run fails
    loudly rather than silently degrading some days to the deterministic
    `reduce_turns` fallback and not others -- a corpus where some days are
    LLM-summarized and others are char-cap'd, indistinguishable from each
    other, was explicitly rejected as worse than a clear failure."""


class OllamaCallError(RuntimeError):
    """One `summarize_day` call failed -- a timeout, a dropped connection
    mid-run, a malformed response -- distinct from `OllamaUnavailableError`
    (nothing reachable at all, checked once up front). Unlike that one,
    this is expected to happen occasionally on an otherwise-working setup
    (a very busy day exceeding even the scaled timeout, a transient
    network hiccup) -- `session_pipeline.py` catches exactly this type to
    fall back to `reduce_turns` for the one affected day and keep going."""


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL


def timeout_for(prompt: str) -> float:
    return BASE_TIMEOUT_SECONDS + len(prompt) / CHARS_PER_SECOND


def _request_json(url: str, *, data: bytes | None, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_available(config: OllamaConfig = OllamaConfig()) -> None:
    """Raise `OllamaUnavailableError` if Ollama isn't reachable or
    `config.model` isn't pulled. A short, fixed timeout here -- this is a
    liveness check, not a generation call, and shouldn't itself hang."""
    try:
        result = _request_json(f"{config.base_url}/api/tags", data=None, timeout=5.0)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise OllamaUnavailableError(f"Ollama not reachable at {config.base_url}: {exc}") from exc
    names = {model.get("name") for model in result.get("models", [])}
    if config.model not in names:
        raise OllamaUnavailableError(
            f"model {config.model!r} not found in Ollama -- pull it with "
            f"`ollama pull {config.model}`"
        )


def _format_turns(turns: list[Turn]) -> str:
    return "\n\n".join(f"[{turn.at.astimezone():%H:%M}] {turn.text}" for turn in turns)


class OllamaSessionProcessor:
    """`Session processor` adapter: one Ollama call per day, `think: false`,
    one prose paragraph out -- the real local-LLM half of the seam
    `ai_sessions.reduce_turns` fills deterministically (CONTEXT.md)."""

    def __init__(self, config: OllamaConfig = OllamaConfig()) -> None:
        self.config = config

    def summarize_day(self, turns: list[Turn]) -> str:
        """Summarize `turns`' assistant turns only -- the benchmark that
        picked `qwen3.5:4b` fed it assistant turns alone and the result was
        accurate against the source; user turns weren't included and
        haven't been validated as an addition. Empty string if there's
        nothing to summarize (an all-user-turn day, or none at all) --
        callers treat that as "nothing to write," same as an empty
        `reduce_turns` result."""
        assistant_turns = [t for t in turns if t.role == ROLE_ASSISTANT]
        if not assistant_turns:
            return ""
        prompt = _PROMPT_TEMPLATE.format(turns=_format_turns(assistant_turns))
        payload = json.dumps(
            {"model": self.config.model, "prompt": prompt, "think": False, "stream": False}
        ).encode("utf-8")
        try:
            result = _request_json(
                f"{self.config.base_url}/api/generate", data=payload, timeout=timeout_for(prompt)
            )
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            raise OllamaCallError(f"Ollama call failed: {exc}") from exc
        return result.get("response", "").strip()

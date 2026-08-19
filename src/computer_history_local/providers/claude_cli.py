"""Summarize one chunk's already-decided spans through the locally installed
`claude` CLI.

Forked from `adhd_lifelog`'s `providers/claude_cli.py`: same subprocess
mechanics, for the same reason -- the subscription is already paid for, and
this spends it instead of a metered API key (`ADR-0007`, `CONTEXT.md`'s
`Memory Pipeline` entry). Trimmed to this project's own, much smaller schema
(one summary sentence per span, not `adhd_lifelog`'s wide `NextStep`), and
without pydantic: the shape is small enough that manual validation keeps
every failure just as loud without a new dependency this project didn't
otherwise need.

One call is one chunk (`ADR-0007`) -- never a whole run's backlog in one
call. Ticket #23 moved span segmentation itself out of this call entirely:
`memory_pipeline.py`'s `build_spans` decides boundaries and apps in code,
and this provider is only ever asked to describe spans it's handed, never to
find them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

CLAUDE_CLI_PROVIDER_ID = "claude-cli"


@dataclass(frozen=True)
class CliInvocation:
    """What was run, so a failure can be reproduced by hand."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class CliRunner(Protocol):
    def __call__(self, command: list[str], *, timeout: float) -> CliInvocation: ...


def subprocess_runner(command: list[str], *, timeout: float) -> CliInvocation:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        # Never leave the child attached to the terminal -- it would inherit
        # our stdin and, if the CLI reads it for any reason, hang until the
        # timeout instead of answering.
        stdin=subprocess.DEVNULL,
    )
    return CliInvocation(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


# Checked only when `shutil.which` finds nothing for the default "claude" --
# never for a custom `executable=`. launchd's own children run under a bare
# PATH (`/usr/bin:/bin:/usr/sbin:/sbin`), the exact gap `adhd_lifelog`'s own
# `claude_cli.py` hit after three silent automatic-firing failures.
_CLAUDE_CANDIDATES: tuple[str, ...] = (
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)


def _resolve_executable(executable: str) -> str | None:
    """Find the CLI without depending on `$PATH`. `None` if nowhere."""
    found = shutil.which(executable)
    if found:
        return found
    if executable != "claude":
        return None
    for candidate in _CLAUDE_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
    return None


@dataclass(frozen=True)
class ClaudeCliConfig:
    """Everything about *how* the CLI is called, in one place -- data, not
    scattered string literals, so a flag change is a one-line edit."""

    executable: str = "claude"
    # Not None: the CLI's own default model asks for extra usage credits on
    # a Pro account and fails. Knowing the working value and making someone
    # else supply it is the homework this project exists to remove.
    model: str | None = "sonnet"
    timeout_seconds: float = 180.0
    # `claude -p` is an agent, not a completion endpoint -- left alone it
    # will spend turns reading files and searching before answering. One
    # day's data is already everything it needs.
    single_shot_args: tuple[str, ...] = ("--max-turns", "1")
    disable_tools: bool = True
    extra_args: tuple[str, ...] = ()

    def command_for(self, prompt: str) -> list[str]:
        command = [self.executable, "-p", prompt, "--output-format", "json"]
        if self.model:
            command += ["--model", self.model]
        command += list(self.single_shot_args)
        if self.disable_tools:
            # `--tools ""`, not `--allowedTools ""` -- the latter is a
            # permission list (which tools are auto-approved), not a
            # disable switch, and leaves every tool reachable.
            # `claude --help`: '--tools ... Use "" to disable all tools'.
            command += ["--tools", ""]
        command += list(self.extra_args)
        return command


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first complete JSON object out of arbitrary CLI output.

    Models wrap JSON in prose or fences even when told not to. Scanning for
    a balanced object is more forgiving than a regex and cannot be fooled by
    a brace inside a string.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        parsed = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        start = -1
                        continue
                    if isinstance(parsed, dict):
                        return parsed
                    start = -1
    return None


def unwrap_cli_envelope(stdout: str) -> str:
    """Return the assistant's text, whether or not the CLI wrapped it.

    `--output-format json` wraps the answer in a result envelope. If that
    shape changes, or plain text arrives instead, fall back to the raw
    output rather than failing -- the answer is still in there.
    """
    envelope = extract_json_object(stdout)
    if envelope is None:
        return stdout
    for key in ("result", "text", "content", "response"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if "summaries" in envelope:
        # The CLI returned the answer itself rather than an envelope.
        return stdout
    return stdout


class DaySummaryState(Enum):
    COMPLETED = "completed"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    SCHEMA_ERROR = "schema_error"


@dataclass(frozen=True)
class DaySummaryResult:
    """`summaries` is positionally matched to the `span_texts` a call was
    given -- `time_range` and `apps` are never part of this result. Ticket
    #23 moved segmentation (and therefore both fields) to deterministic
    code in `memory_pipeline.py`; the model's only remaining job is writing
    one summary sentence per already-decided span."""

    state: DaySummaryState
    summaries: tuple[str, ...] = ()
    error_message: str | None = None


def _parse_summaries(parsed: dict[str, Any]) -> list[str] | None:
    """Manual validation instead of a schema library: the shape is one field
    deep now that `time_range`/`apps` are code-computed (ticket #23), and a
    hand-written check keeps failures just as specific (`SCHEMA_ERROR` names
    exactly what didn't match) without a new dependency."""
    raw_summaries = parsed.get("summaries")
    if not isinstance(raw_summaries, list):
        return None
    if not all(isinstance(item, str) for item in raw_summaries):
        return None
    return raw_summaries


@dataclass(frozen=True)
class AnswerResult:
    """One free-text answer to an `ask` question (ticket #29), covering
    every existing `Daily memory` file. `cited_dates` may legitimately be
    empty -- a truthful "not found in what's recorded" answer cites
    nothing -- but `answer` itself is never blank (`_parse_answer`
    rejects that)."""

    state: DaySummaryState
    answer: str = ""
    cited_dates: tuple[str, ...] = ()
    error_message: str | None = None


def _parse_answer(parsed: dict[str, Any]) -> tuple[str, list[str]] | None:
    """Manual validation, same reasoning as `_parse_summaries`. `cited_dates`
    may be an empty list (a truthful "I don't see this recorded" has
    nothing to cite) but must still be present and shaped correctly --
    ticket #29's traceability requirement is about *loud* omission, not
    forcing a citation onto an answer that shouldn't have one."""
    answer = parsed.get("answer")
    cited_dates = parsed.get("cited_dates")
    if not isinstance(answer, str) or not answer.strip():
        return None
    if not isinstance(cited_dates, list) or not all(isinstance(d, str) for d in cited_dates):
        return None
    return answer, cited_dates


@dataclass
class ClaudeCliProvider:
    """Summarizes one chunk's already-decided spans per call via a
    subprocess -- one summary sentence per span, in order (`ADR-0007`'s
    "one call per chunk" unchanged; a chunk with several spans still costs
    one call, ticket #23's own resolution)."""

    config: ClaudeCliConfig = field(default_factory=ClaudeCliConfig)
    runner: CliRunner = subprocess_runner
    provider_id: str = CLAUDE_CLI_PROVIDER_ID

    def build_prompt(
        self, day: date, span_texts: list[str], *, window_start: datetime, window_end: datetime
    ) -> str:
        # Always states the actual window, whether it happens to be the
        # whole day or a chunk of it (ticket #15): the model is never told
        # "summarize the day" while holding only a fraction of it, which
        # otherwise leaves no signal against narrating past this call's own
        # slice or inventing what happened in the hours it wasn't given.
        numbered_spans = "\n\n".join(
            f"Span {index + 1}:\n{text}" for index, text in enumerate(span_texts)
        )
        return (
            "You have no tools. Do not attempt to read files, run commands, "
            "or search. Everything you need is in the data below.\n\n"
            f"Below are {len(span_texts)} already-decided spans of this "
            "person's activity between "
            f"{window_start.astimezone():%H:%M} and {window_end.astimezone():%H:%M} "
            f"on {day.isoformat()}, based on app/window data. Span "
            "boundaries are fixed -- yours is only to describe what each one "
            "shows. Write exactly one summary sentence per span, in the same "
            "order, based only on that span's own data below; do not invent "
            "detail the data doesn't support.\n\n"
            "Answer with a single JSON object and nothing else. No prose, "
            "no markdown fence, no explanation. Shape:\n"
            '{"summaries": ["...", "..."]}\n\n'
            f"{numbered_spans}\n"
        )

    def _invoke_and_parse(
        self, prompt: str
    ) -> tuple[dict[str, Any], None] | tuple[None, tuple[DaySummaryState, str]]:
        """Everything `summarize()` and `answer()` share: resolve the CLI,
        run it, and get back a parsed JSON object or a specific failure --
        the two callers only ever differ in what shape they expect *inside*
        that object. Factored out (ticket #29) rather than duplicated,
        since `answer()` needed the exact same subprocess mechanics as
        `summarize()` already had, already tested."""
        resolved = _resolve_executable(self.config.executable)
        if resolved is None:
            return None, (
                DaySummaryState.PROVIDER_ERROR,
                f"{self.config.executable} is not on PATH.",
            )

        command = self.config.command_for(prompt)
        command[0] = resolved
        try:
            invocation = self.runner(command, timeout=self.config.timeout_seconds)
        except subprocess.TimeoutExpired:
            return None, (
                DaySummaryState.TIMEOUT,
                f"{self.config.executable} did not finish within "
                f"{self.config.timeout_seconds:g}s.",
            )
        except OSError as exc:
            return None, (DaySummaryState.PROVIDER_ERROR, str(exc))

        if invocation.returncode != 0:
            detail = (invocation.stderr or invocation.stdout or "").strip()
            return None, (
                DaySummaryState.PROVIDER_ERROR,
                f"{self.config.executable} exited {invocation.returncode}: "
                + (detail[:400] or "no output"),
            )

        envelope = extract_json_object(invocation.stdout)
        if isinstance(envelope, dict) and envelope.get("is_error"):
            detail = envelope.get("result")
            return None, (
                DaySummaryState.PROVIDER_ERROR,
                f"{self.config.executable} reported an error: "
                + (str(detail)[:400] if detail else "no detail"),
            )

        answer_text = unwrap_cli_envelope(invocation.stdout)
        parsed = extract_json_object(answer_text)
        if parsed is None:
            return None, (
                DaySummaryState.SCHEMA_ERROR,
                "no JSON object found in the CLI output. First 400 "
                "characters: " + invocation.stdout.strip()[:400],
            )
        return parsed, None

    def summarize(
        self, day: date, span_texts: list[str], *, window_start: datetime, window_end: datetime
    ) -> DaySummaryResult:
        prompt = self.build_prompt(day, span_texts, window_start=window_start, window_end=window_end)
        parsed, error = self._invoke_and_parse(prompt)
        if error is not None:
            state, message = error
            return DaySummaryResult(state=state, error_message=message)

        summaries = _parse_summaries(parsed)
        if summaries is None:
            return DaySummaryResult(
                state=DaySummaryState.SCHEMA_ERROR,
                error_message="CLI output did not match the {summaries: [\"...\"]} shape.",
            )

        # `_process_day` zips these positionally against the spans that
        # produced `span_texts` -- a mismatched count would misalign every
        # summary after the gap, silently (ticket #23's own flagged
        # constraint on this ticket). Caught here, loud, before it reaches
        # that zip.
        if len(summaries) != len(span_texts):
            return DaySummaryResult(
                state=DaySummaryState.SCHEMA_ERROR,
                error_message=(
                    f"expected {len(span_texts)} summaries (one per span), got {len(summaries)}."
                ),
            )

        return DaySummaryResult(state=DaySummaryState.COMPLETED, summaries=tuple(summaries))

    def build_answer_prompt(
        self, question: str, day_contents: list[tuple[date, str]], *, today: date
    ) -> str:
        # States today's real date explicitly, the same reason
        # `build_prompt` states the actual window: resolving a relative
        # phrase ("last week", "when I fixed that bug") needs a stated
        # calendar anchor, not just the Daily memory entries' own dates
        # (ticket #29).
        daily_memories = "\n\n".join(content for _day, content in day_contents)
        return (
            "You have no tools. Do not attempt to read files, run commands, "
            "or search. Everything you need is in the data below.\n\n"
            f"Today's date is {today.isoformat()}. Answer the question below "
            "using only the Daily memory entries provided -- each entry "
            "starts with its own '# YYYY-MM-DD' date heading. Resolve "
            "relative time phrases against today's date and the entries' "
            "own dates. If the answer isn't supported by the data below, "
            "say so plainly rather than guessing.\n\n"
            "Answer with a single JSON object and nothing else. No prose, "
            "no markdown fence, no explanation. Shape:\n"
            '{"answer": "...", "cited_dates": ["YYYY-MM-DD", ...]}\n\n'
            f"Question: {question}\n\n"
            f"Data:\n{daily_memories}\n"
        )

    def answer(
        self, question: str, day_contents: list[tuple[date, str]], *, today: date
    ) -> AnswerResult:
        prompt = self.build_answer_prompt(question, day_contents, today=today)
        parsed, error = self._invoke_and_parse(prompt)
        if error is not None:
            state, message = error
            return AnswerResult(state=state, error_message=message)

        parsed_answer = _parse_answer(parsed)
        if parsed_answer is None:
            return AnswerResult(
                state=DaySummaryState.SCHEMA_ERROR,
                error_message=(
                    'CLI output did not match the {answer: "...", '
                    'cited_dates: ["..."]} shape.'
                ),
            )
        text, cited_dates = parsed_answer
        return AnswerResult(
            state=DaySummaryState.COMPLETED, answer=text, cited_dates=tuple(cited_dates)
        )

"""Summarize one day through the locally installed `claude` CLI.

Forked from `adhd_lifelog`'s `providers/claude_cli.py`: same subprocess
mechanics, for the same reason -- the subscription is already paid for, and
this spends it instead of a metered API key (`ADR-0007`, `CONTEXT.md`'s
`Memory Pipeline` entry). Trimmed to this project's own, much smaller schema
(a day's timeline entries, not `adhd_lifelog`'s wide `NextStep`), and without
pydantic: the shape is small enough that manual validation keeps every
failure just as loud without a new dependency this project didn't otherwise
need.

One call is one calendar day (or, once ticket #15 lands, one sub-day chunk)
-- never a whole run's backlog in one call. See `ADR-0007`.
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
    if "entries" in envelope:
        # The CLI returned the answer itself rather than an envelope.
        return stdout
    return stdout


class DaySummaryState(Enum):
    COMPLETED = "completed"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    SCHEMA_ERROR = "schema_error"


@dataclass(frozen=True)
class TimelineEntry:
    """One structured entry in a `Daily memory` -- CONTEXT.md's "time range →
    summary → contributing apps" shape, not free-form prose."""

    time_range: str
    summary: str
    apps: tuple[str, ...] = ()


@dataclass(frozen=True)
class DaySummaryResult:
    state: DaySummaryState
    entries: tuple[TimelineEntry, ...] = ()
    error_message: str | None = None


def _parse_entries(parsed: dict[str, Any]) -> list[TimelineEntry] | None:
    """Manual validation instead of a schema library: the shape is three
    fields deep, and a hand-written check keeps failures just as specific
    (`SCHEMA_ERROR` names exactly what didn't match) without adding a new
    dependency this project didn't otherwise need."""
    raw_entries = parsed.get("entries")
    if not isinstance(raw_entries, list):
        return None
    entries: list[TimelineEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            return None
        time_range = item.get("time_range")
        summary = item.get("summary")
        apps = item.get("apps", [])
        if not isinstance(time_range, str) or not isinstance(summary, str):
            return None
        if not isinstance(apps, list) or not all(isinstance(app, str) for app in apps):
            return None
        entries.append(TimelineEntry(time_range=time_range, summary=summary, apps=tuple(apps)))
    return entries


@dataclass
class ClaudeCliProvider:
    """Summarizes one day (or sub-day chunk) per call via a subprocess."""

    config: ClaudeCliConfig = field(default_factory=ClaudeCliConfig)
    runner: CliRunner = subprocess_runner
    provider_id: str = CLAUDE_CLI_PROVIDER_ID

    def build_prompt(
        self, day: date, day_text: str, *, window_start: datetime, window_end: datetime
    ) -> str:
        # Always states the actual window, whether it happens to be the
        # whole day or a chunk of it (ticket #15): the model is never told
        # "summarize the day" while holding only a fraction of it, which
        # otherwise leaves no signal against narrating past this call's own
        # slice or inventing what happened in the hours it wasn't given.
        return (
            "You have no tools. Do not attempt to read files, run commands, "
            "or search. Everything you need is in the data below.\n\n"
            "Summarize this person's activity between "
            f"{window_start.astimezone():%H:%M} and {window_end.astimezone():%H:%M} "
            f"on {day.isoformat()}, based only on the app/window/idle/browser "
            "data below. This may be only part of the day -- other separate "
            "calls cover the rest, so do not comment on or invent activity "
            "outside this window. Group nearby activity into a small number "
            "of time-range entries within this window; do not invent detail "
            "the data doesn't support.\n\n"
            "Answer with a single JSON object and nothing else. No prose, "
            "no markdown fence, no explanation. Shape:\n"
            '{"entries": [{"time_range": "HH:MM–HH:MM", "summary": '
            '"...", "apps": ["..."]}]}\n\n'
            f"Data:\n{day_text}\n"
        )

    def summarize(
        self, day: date, day_text: str, *, window_start: datetime, window_end: datetime
    ) -> DaySummaryResult:
        def failure(state: DaySummaryState, message: str) -> DaySummaryResult:
            return DaySummaryResult(state=state, error_message=message)

        resolved = _resolve_executable(self.config.executable)
        if resolved is None:
            return failure(
                DaySummaryState.PROVIDER_ERROR,
                f"{self.config.executable} is not on PATH.",
            )

        prompt = self.build_prompt(day, day_text, window_start=window_start, window_end=window_end)
        command = self.config.command_for(prompt)
        command[0] = resolved
        try:
            invocation = self.runner(command, timeout=self.config.timeout_seconds)
        except subprocess.TimeoutExpired:
            return failure(
                DaySummaryState.TIMEOUT,
                f"{self.config.executable} did not finish within "
                f"{self.config.timeout_seconds:g}s.",
            )
        except OSError as exc:
            return failure(DaySummaryState.PROVIDER_ERROR, str(exc))

        if invocation.returncode != 0:
            detail = (invocation.stderr or invocation.stdout or "").strip()
            return failure(
                DaySummaryState.PROVIDER_ERROR,
                f"{self.config.executable} exited {invocation.returncode}: "
                + (detail[:400] or "no output"),
            )

        envelope = extract_json_object(invocation.stdout)
        if isinstance(envelope, dict) and envelope.get("is_error"):
            detail = envelope.get("result")
            return failure(
                DaySummaryState.PROVIDER_ERROR,
                f"{self.config.executable} reported an error: "
                + (str(detail)[:400] if detail else "no detail"),
            )

        answer = unwrap_cli_envelope(invocation.stdout)
        parsed = extract_json_object(answer)
        if parsed is None:
            return failure(
                DaySummaryState.SCHEMA_ERROR,
                "no JSON object found in the CLI output. First 400 "
                "characters: " + invocation.stdout.strip()[:400],
            )

        entries = _parse_entries(parsed)
        if entries is None:
            return failure(
                DaySummaryState.SCHEMA_ERROR,
                "CLI output did not match the {entries: [{time_range, "
                "summary, apps}]} shape.",
            )

        return DaySummaryResult(state=DaySummaryState.COMPLETED, entries=tuple(entries))

"""Discover, parse, and reduce `AI session` transcripts -- ticket #31.

Reads Claude Code's and Codex's own local session files
(`~/.claude/projects/**/*.jsonl`, `~/.codex/sessions/**/*.jsonl`) --
never written by this project, only discovered and read. `CONTEXT.md`'s
`AI session` entry: this is the point of the whole feature, not an add-on --
Computer History Local exists to mirror OpenAI's Computer History, and
reading an agent's own past sessions back is central to that.

Kept separate from `session_pipeline.py`/`session_store.py`: this module
only ever reads source files and reduces text in memory. It never persists
anything -- the cursor/slice/index bookkeeping that makes reduction
incremental lives in `session_store.py`, driven by `session_pipeline.py`.

The reduction here (`reduce_turns`) is v1's one `Session processor` adapter:
deterministic, no model call, adapted from `adhd_lifelog`'s own AI-session
evidence design (role-capped budget, head+tail truncation, drop order) but
without its anchor-picking step -- a real local model is the intended second
adapter behind this same seam (`docs/later.md`), not a fancier deterministic
one, so v1 keeps this half of the seam as simple as it can be.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

TOOL_CLAUDE_CODE = "claude_code"
TOOL_CODEX = "codex"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

DEFAULT_SESSION_ROOTS: tuple[tuple[Path, str], ...] = (
    (Path("~/.claude/projects"), TOOL_CLAUDE_CODE),
    (Path("~/.codex/sessions"), TOOL_CODEX),
)

# Claude Code exposes its own session id verbatim as an env var, and it
# matches a session file's stem byte-for-byte (verified directly: a running
# session's `CLAUDE_CODE_SESSION_ID` equals the `.jsonl` file's name minus
# the extension, in the workspace-scoped project directory it writes to).
# Codex exposes no verified equivalent -- `CODEX_SESSION_ID` here is a
# best-effort name, not a confirmed one; if it's never set, a Codex session
# is simply never treated as live (CONTEXT.md's `Live session`), which is
# the safe direction to be wrong in (worst case, a redundant session is
# read; never a session silently dropped from evidence).
_CURRENT_SESSION_ENV_VARS: dict[str, str] = {
    TOOL_CLAUDE_CODE: "CLAUDE_CODE_SESSION_ID",
    TOOL_CODEX: "CODEX_SESSION_ID",
}


@dataclass(frozen=True)
class SessionFile:
    """One discovered session file. `path` is absolute; `tool` picks which
    parser (`PARSERS`) and which live-session check applies."""

    path: Path
    tool: str


def discover_session_files(
    roots: tuple[tuple[Path, str], ...] = DEFAULT_SESSION_ROOTS,
) -> list[SessionFile]:
    """Every `*.jsonl` under each configured root, sorted by path for
    deterministic ordering. A root that doesn't exist (e.g. Codex never
    installed on this Mac) is skipped silently -- its absence is the normal
    case for a person who only uses one of the two tools, not an error."""
    files: list[SessionFile] = []
    for root, tool in roots:
        expanded = root.expanduser()
        if not expanded.is_dir():
            continue
        files.extend(SessionFile(path=path, tool=tool) for path in sorted(expanded.rglob("*.jsonl")))
    return files


def current_session_id(tool: str) -> str | None:
    env_var = _CURRENT_SESSION_ENV_VARS.get(tool)
    if env_var is None:
        return None
    return os.environ.get(env_var) or None


def is_live_session(session_file: SessionFile) -> bool:
    """Whether `session_file` is the one still open right now -- the one
    `AI session` CONTEXT.md's `Live session` entry always excludes, singular,
    because it's redundant with what's already in the calling agent's own
    context, not because it's untrustworthy."""
    session_id = current_session_id(session_file.tool)
    if session_id is None:
        return False
    if session_file.tool == TOOL_CLAUDE_CODE:
        return session_file.path.stem == session_id
    # Codex file names are `rollout-<timestamp>-<uuid>.jsonl`, not a bare
    # uuid stem -- the id is a substring, not the whole name.
    return session_id in session_file.path.stem


@dataclass(frozen=True)
class Turn:
    """One user or assistant message, reduced to its text and timestamp.
    Tool calls, tool results, and thinking blocks are dropped entirely at
    parse time, not just left empty -- they're noise for this reduction's
    purpose (what was this person doing/asking), and keeping them would
    only inflate what `reduce_turns` has to budget against."""

    role: str
    text: str
    at: datetime


def _claude_code_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_claude_code_turns(lines: Iterable[str]) -> list[Turn]:
    """Claude Code's JSONL: one record per line, `type` is `"user"` or
    `"assistant"` for a message (also `"mode"`, `"attachment"`,
    `"file-history-snapshot"`, etc., which aren't messages at all and are
    skipped). `isSidechain` marks a subagent's own turn, not the main
    conversation the person actually had -- excluded, same reasoning
    `adhd_lifelog`'s own harness-noise filtering uses."""
    turns: list[Turn] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") not in (ROLE_USER, ROLE_ASSISTANT):
            continue
        if record.get("isSidechain"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        text = _claude_code_text(message.get("content")).strip()
        if not text:
            continue
        at = _parse_timestamp(record.get("timestamp"))
        if at is None:
            continue
        turns.append(Turn(role=record["type"], text=text, at=at))
    return turns


_CODEX_ROLE_MAP: dict[str, str] = {"user": ROLE_USER, "assistant": ROLE_ASSISTANT}
# "developer" role records exist too (harness/system-prompt-injected
# instructions) -- deliberately absent from this map so they're skipped,
# not attributed to either the person or the assistant.


def _codex_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text")
    ]
    return "\n".join(part for part in parts if part)


def parse_codex_turns(lines: Iterable[str]) -> list[Turn]:
    """Codex's JSONL: one record per line, a message lives at
    `payload.type == "message"` inside a `type == "response_item"` record
    (Codex also logs `event_msg`, `turn_context`, `session_meta`,
    `world_state`, `compacted` records -- none are conversation turns)."""
    turns: list[Turn] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = _CODEX_ROLE_MAP.get(payload.get("role"))
        if role is None:
            continue
        text = _codex_text(payload.get("content")).strip()
        if not text:
            continue
        at = _parse_timestamp(record.get("timestamp"))
        if at is None:
            continue
        turns.append(Turn(role=role, text=text, at=at))
    return turns


PARSERS = {TOOL_CLAUDE_CODE: parse_claude_code_turns, TOOL_CODEX: parse_codex_turns}


@dataclass(frozen=True)
class NewTurns:
    """`read_new_turns`'s result: the turns found past `from_offset`, and
    the byte offset to resume from next time."""

    turns: list[Turn]
    new_offset: int


def read_new_turns(session_file: SessionFile, *, from_offset: int = 0) -> NewTurns:
    """Read and parse only the complete lines appended after `from_offset`
    -- `Session cursor`'s whole point (CONTEXT.md): a resumed session only
    costs re-parsing its new tail, not the whole file, on the next
    `process-sessions` run.

    A trailing partial line (the file is being actively written by a still-
    open session) is left unconsumed rather than parsed half-formed or
    dropped -- `new_offset` stops just past the last complete line, so the
    partial one is picked up whole next time it's actually complete. A
    `from_offset` past the current file length (the file was truncated or
    replaced since the cursor was recorded, not something this project's
    own append-only sources ever do, but not this module's file to trust
    blindly either) restarts from the beginning rather than raising.

    Reads via `seek`, not a whole-file `read_bytes()` -- the entire point of
    a byte offset (`Session cursor`) is to avoid re-reading bytes already
    consumed. Against a resumed session with megabytes of prior turns, the
    difference is real: `seek` costs one syscall regardless of file size,
    where `read_bytes()` would re-read the whole file on every run just to
    throw most of it away.
    """
    size = session_file.path.stat().st_size
    offset = from_offset if from_offset <= size else 0
    with session_file.path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    if not chunk:
        return NewTurns(turns=[], new_offset=offset)

    last_newline = chunk.rfind(b"\n")
    if last_newline == -1:
        return NewTurns(turns=[], new_offset=offset)

    complete = chunk[: last_newline + 1]
    new_offset = offset + len(complete)
    lines = complete.decode("utf-8", errors="replace").splitlines()
    parser = PARSERS[session_file.tool]
    return NewTurns(turns=parser(lines), new_offset=new_offset)


def turns_to_json(turns: list[Turn]) -> str:
    """Serialize `turns` for storage as a `Session slice`'s raw form --
    kept alongside the already-reduced text (`session_store.SessionSlice`)
    so a later incremental update can re-run `reduce_turns` over the *whole*
    day's turns (old + newly appended) rather than only the new ones, which
    would let a slice's budget silently drift upward across repeated
    `process-sessions` runs on a resumed session."""
    return json.dumps([{"role": t.role, "text": t.text, "at": t.at.isoformat()} for t in turns])


def turns_from_json(raw: str) -> list[Turn]:
    return [
        Turn(role=item["role"], text=item["text"], at=datetime.fromisoformat(item["at"]))
        for item in json.loads(raw)
    ]


def turns_by_local_day(turns: list[Turn]) -> dict[date, list[Turn]]:
    """Group turns by the local calendar day each one's own timestamp falls
    on. A turn is a point in time, not an interval like a `State sample`
    row -- unlike `memory_pipeline.rows_for_day`, no interval-overlap
    duplication across a midnight boundary is needed here."""
    buckets: dict[date, list[Turn]] = {}
    for turn in sorted(turns, key=lambda t: t.at):
        buckets.setdefault(turn.at.astimezone().date(), []).append(turn)
    return buckets


# Role-based character caps within one reduction -- adapted from
# adhd_lifelog's own AI-session evidence design (a user's own words get the
# most room; an assistant turn is more reconstructable from what it did).
# Tuned down from that project's values since this is a v1 placeholder
# standing in for a real local model (`Session processor`, docs/later.md),
# not the final algorithm -- getting this exactly right isn't the point.
ROLE_CHAR_CAP: dict[str, int] = {ROLE_USER: 2000, ROLE_ASSISTANT: 1200}

# Total budget for one `Session slice` (one session file's turns for one
# day) -- same order of magnitude as memory_pipeline.py's own
# DEFAULT_CONTENT_CAP_CHARS, for the same "keep one unit of work small"
# reasoning (ADR-0007), not a hard limit derived from anything else.
SLICE_CHAR_BUDGET = 4000

_TRUNCATION_MARKER = " …[truncated]… "


def _cap_turn_text(text: str, cap: int) -> str:
    """Head + tail, not a blunt prefix cut -- a turn typically opens with
    what it set out to do and closes with what it concluded (the same
    observation `adhd_lifelog`'s own reduction design made); losing the
    middle keeps both ends legible."""
    if len(text) <= cap:
        return text
    head_len = max(0, int(cap * 0.65) - len(_TRUNCATION_MARKER))
    tail_len = max(0, cap - head_len - len(_TRUNCATION_MARKER))
    return text[:head_len] + _TRUNCATION_MARKER + (text[-tail_len:] if tail_len else "")


def reduce_turns(turns: list[Turn], *, budget: int = SLICE_CHAR_BUDGET) -> str:
    """v1's one `Session processor` adapter (CONTEXT.md): deterministic, no
    model call. Caps each turn to its role's limit, then drops whole turns
    -- assistant first, user only once no assistant turns remain, and never
    the last turn standing -- until the combined text fits `budget`. Same
    drop order `adhd_lifelog` uses, for the same reason: an assistant turn's
    own words are the more reconstructable half of a conversation; a user
    turn is the one thing nothing else can stand in for."""
    capped = [
        (turn, _cap_turn_text(turn.text, ROLE_CHAR_CAP.get(turn.role, ROLE_CHAR_CAP[ROLE_ASSISTANT])))
        for turn in turns
    ]

    def total_chars(items: list[tuple[Turn, str]]) -> int:
        if not items:
            return 0
        return sum(len(text) for _turn, text in items) + (len(items) - 1) * 2  # "\n\n" joins

    while total_chars(capped) > budget and len(capped) > 1:
        assistant_indices = [i for i, (turn, _text) in enumerate(capped) if turn.role == ROLE_ASSISTANT]
        del capped[assistant_indices[0] if assistant_indices else 0]

    return "\n\n".join(f"{turn.role}: {text}" for turn, text in capped)


@dataclass(frozen=True)
class Section:
    """One tool's reduced text for one day -- the shared unit
    `render_day_markdown` renders, whether it came from a persisted
    `Session slice` (`session_pipeline.py`) or a `Live session`-excluded,
    never-persisted read of today's files (`retrieval.py`). Keeping one
    render function for both is what keeps a cached day and a live-read
    "today" look like the same kind of thing to whoever reads the output."""

    tool: str
    start_at: datetime
    end_at: datetime
    text: str


def render_day_markdown(day: str, sections: list[Section]) -> str:
    """`Section`s for one day -> the `AI session` Markdown for that day --
    each section its own heading (tool + time range), never merged into one
    undifferentiated block (`CONTEXT.md`'s `Retrieval` entry: the same
    separation principle applies within this text, not just between it and
    `Daily memory`)."""
    lines = [f"# AI sessions — {day}", ""]
    for section in sections:
        start_local = section.start_at.astimezone()
        end_local = section.end_at.astimezone()
        lines.append(f"## {section.tool} — {start_local:%H:%M}–{end_local:%H:%M}")
        lines.append("")
        lines.append(section.text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

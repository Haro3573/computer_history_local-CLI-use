"""Read the machine's own sleep/wake log, after the fact.

Ported from `adhd_lifelog`'s `power_log.py`: same reasoning. Every other
channel here is a poll -- ask what's true right now -- so a resident
process's own gaps can't say whether they were the machine asleep, the
person away, or the Collector dead. `pmset -g log` is a record macOS keeps
whether or not anything of ours is running, so the events that happen
*while the Collector is stopped* -- exactly the interesting ones -- can be
read back later. No resident process, no notification subscription, no new
permission, and it can label gaps recorded before this file existed.

Surfaced by a real need, not spec-following: resolving ticket #23 (span
segmentation) required knowing whether a real overnight gap in the window
channel was the machine genuinely asleep or the person merely away, and the
only way to check was running `pmset -g log` by hand. This channel makes
that check part of the record instead of a one-off.

Adapted to this project's flat `(kind, at, duration, app, title, url, away)`
`State sample` shape (`Observation`/`Row`) rather than `adhd_lifelog`'s
`extra`-dict `Event`: `app` carries the pmset entry type (`sleep`/`wake`/
`darkwake`), `title` carries the reason string exactly as pmset printed it.
Deliberately does not decide what a reason means -- `state_samples` is
effectively append-only (`ADR-0003`), so a reason misclassified today can't
be reclassified tomorrow; the reading of it happens at query time instead.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime

from .store import KIND_SYSTEM, Observation, Insert, Store

PMSET = "/usr/bin/pmset"

# 2026-01-15 13:38:04 +0900 Sleep               \tEntering Sleep state due to...
#
# The entry type is a padded column, read as the whole column rather than a
# prefix -- `Wake Requests` is a different entry type from `Wake` (the
# system *scheduling* a future wake, not waking), and matching the first
# word alone turns every `Wake Requests` into a wake seconds after a sleep.
_LINE = re.compile(
    r"^(?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})\s+"
    r"(?P<what>\S+(?: \S+)*?)(?:\t|\s{2,})(?P<reason>.*?)\s*$"
)

# The column values this reads. Anything else pmset logs -- Assertions,
# Notification, Wake Requests, battery samples -- is about something other
# than whether the machine was available, and is skipped rather than
# reshaped.
ENTRY_TYPES = {"Sleep", "Wake", "DarkWake"}


def parse_power_log(text: str) -> list[Observation]:
    """Turn pmset's log into `Observation`s, keeping every reason verbatim.

    Lines that are not sleep or wake are skipped rather than reported: the
    log also carries assertions, thermal notices, and battery samples, none
    of which are about whether the machine was available.
    """
    observations: list[Observation] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if match is None or match["what"] not in ENTRY_TYPES:
            continue
        try:
            at = datetime.strptime(match["when"], "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            # A malformed timestamp is a line that can't be placed in time,
            # and an event at the wrong time is worse than no event.
            continue
        observations.append(
            Observation(kind=KIND_SYSTEM, at=at, app=match["what"].lower(), title=match["reason"])
        )
    return observations


def read_power_log(*, timeout: float = 20.0) -> tuple[str | None, str | None]:
    """Run pmset. Returns (output, error); never raises, like the sampler.

    Needs no entitlement and no TCC prompt -- it's the same command any user
    can run in a terminal.
    """
    try:
        completed = subprocess.run(
            [PMSET, "-g", "log"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return None, (completed.stderr or "").strip()[:200] or f"exit {completed.returncode}"
    return completed.stdout, None


def ingest_power_events(store: Store, *, text: str | None = None) -> tuple[int, str | None]:
    """Store every sleep/wake newer than the newest one already stored.

    Returns (rows written, error). Re-running is a no-op past what's new,
    which is what lets this be called on a timer with no cursor file of its
    own: the store already knows how far it got, and `pmset` will happily
    print the same history again every call.
    """
    if text is None:
        text, error = read_power_log()
        if text is None:
            return 0, error

    previous = store.last_row(KIND_SYSTEM)
    since = previous.at if previous is not None else None

    written = 0
    for observation in parse_power_log(text):
        # Strictly newer: an event at exactly the stored timestamp is the
        # one already stored. `record()` only suppresses *consecutive
        # identical* states, and two sleeps with different reasons are not
        # identical, so without this the whole log would be re-inserted on
        # every call.
        if since is not None and observation.at <= since:
            continue
        if isinstance(store.record(observation), Insert):
            written += 1
    return written, None

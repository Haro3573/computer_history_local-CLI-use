"""Wire the sampler to the store: poll, redact, record.

Kept thin and separate from `sampler.py` and `store.py` so a fake sampler can
drive this loop end-to-end in tests -- the class of bug a passing unit-test
suite alone won't catch (`adhd_lifelog`'s `claude-cli` provider passed every
unit test and still failed at the entry point).
"""

from __future__ import annotations

import time
from typing import Callable

from .redaction import DeterministicRedactor
from .sampler import Sample, sanitize_browser_url
from .sampler import sample as default_sample
from .store import KIND_BROWSER, KIND_IDLE, KIND_WINDOW, Observation, Store

DEFAULT_INTERVAL_SECONDS = 30.0

# Past this you are not at the computer. Inherited from `mac_sampler.py`'s
# own value rather than guessed fresh: "chosen to match the shortest gap that
# reliably means 'left the desk' rather than 'read a paragraph'."
DEFAULT_IDLE_THRESHOLD_SECONDS = 180.0


def sample_to_events(
    reading: Sample, *, idle_threshold: float = DEFAULT_IDLE_THRESHOLD_SECONDS
) -> list[Observation]:
    """Turn one reading into the independent facts it contains.

    Idle/away, the frontmost window, and the browser URL are three
    independent facts (tickets #3, #4): being away suppresses none of
    them -- unlike `adhd_lifelog`'s `sample_to_events`, which deliberately
    suppresses window and browser both under `if not away`. Redaction and
    URL sanitization happen here, before an `Observation` is ever
    constructed -- masking after the row is written would be too late (see
    `redaction.py`).
    """
    observations: list[Observation] = []

    away = reading.idle_seconds >= idle_threshold
    observations.append(Observation(kind=KIND_IDLE, at=reading.at, away=away))

    redactor = DeterministicRedactor()

    if reading.app:
        title = reading.title
        if title:
            title, _ = redactor.redact(title)
        observations.append(
            Observation(kind=KIND_WINDOW, at=reading.at, app=reading.app, title=title)
        )

    url = sanitize_browser_url(reading.url)
    if url:
        url, _ = redactor.redact(url)
        observations.append(
            Observation(kind=KIND_BROWSER, at=reading.at, app=reading.app, url=url)
        )

    return observations


def run_once(
    store: Store, *, sample_fn: Callable[[], Sample] = default_sample
) -> list[Observation]:
    """One poll -> zero or more observations -> recorded into `store`."""
    reading = sample_fn()
    observations = sample_to_events(reading)
    for observation in observations:
        store.record(observation)
    return observations


def run_forever(
    store: Store,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    sample_fn: Callable[[], Sample] = default_sample,
) -> None:
    from .power_log import ingest_power_events

    # Sleep/wake is read back from the machine's own log, not sampled, so it
    # doesn't belong in every 30-second poll -- pmset reprints its entire
    # history on every call. Ten minutes (`adhd_lifelog`'s own cadence,
    # ticket #26) is far below the gaps this is meant to label.
    power_every = max(1, round(600 / interval_seconds))
    polls = 0
    while True:
        run_once(store, sample_fn=sample_fn)
        if polls % power_every == 0:
            ingest_power_events(store)
        polls += 1
        time.sleep(interval_seconds)

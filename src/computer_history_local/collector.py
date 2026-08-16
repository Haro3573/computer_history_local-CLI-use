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
from .sampler import Sample
from .sampler import sample as default_sample
from .store import KIND_WINDOW, Observation, Store

DEFAULT_INTERVAL_SECONDS = 30.0


def sample_to_events(reading: Sample) -> list[Observation]:
    """Turn one reading into the window-kind observation it contains, if any.

    Redaction happens here, before an `Observation` is ever constructed --
    masking after the row is written would be too late (see `redaction.py`).
    """
    if not reading.app:
        return []
    title = reading.title
    if title:
        redactor = DeterministicRedactor()
        title, _ = redactor.redact(title)
    return [Observation(kind=KIND_WINDOW, at=reading.at, app=reading.app, title=title)]


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
    while True:
        run_once(store, sample_fn=sample_fn)
        time.sleep(interval_seconds)

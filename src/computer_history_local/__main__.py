"""Run the collector loop against the default local store.

    python -m computer_history_local
"""

from .collector import run_forever
from .store import DEFAULT_STORE, Store

if __name__ == "__main__":
    with Store(DEFAULT_STORE) as store:
        run_forever(store)

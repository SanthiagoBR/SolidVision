"""The warm-up of the models, in a thread, and what `/health` says of it (RFC-036).

The API answers in two times (section 5.3). The port opens as soon as the API
is imported, which no longer imports `torch`; the warm-up of RFC-026 section
10 runs afterwards in a daemon thread, and this is where it says how it went.

    search        when
    loading       the warm-up is running
    ready         it ended, and both models are loaded
    unavailable   it failed with `ModelUnavailableError`; the detail is the line
    on_demand     it is off, or failed for another reason: the first search loads

"Ready" in `/health` does not depend on it (section 5.2): a 200 with `search:
unavailable` is the API of RFC-035 section 6.5, which shows the collection and
cannot search it. What the window draws for each value is the frontend's.

Here and not in `app.presentation.api`, so that the route that reads it does
not import the package that includes the route.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class SearchState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    ON_DEMAND = "on_demand"


@dataclass(frozen=True)
class SearchReadiness:
    state: SearchState
    detail: str | None = None
    """The line of RFC-035 section 6.4, when `state` is `unavailable`."""


class ModelWarmUp:
    """Runs the warm-up in a daemon thread, and remembers what it came to.

    Daemon, so that a process asked to stop while the thread is still
    importing `torch` -- 14 to 40 s on the hard disk of the measurements -- is
    not held up by it: the stop of RFC-036 section 7.2 gives the API 15 s.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._readiness = SearchReadiness(SearchState.ON_DEMAND)
        self._thread: threading.Thread | None = None

    def readiness(self) -> SearchReadiness:
        with self._lock:
            return self._readiness

    def skip(self) -> None:
        """The warm-up is off: the first search loads the models."""
        with self._lock:
            self._readiness = SearchReadiness(SearchState.ON_DEMAND)

    def start(self, warm_up: Callable[[], SearchReadiness]) -> threading.Thread:
        """Run `warm_up` in a thread, unless one is already running.

        `warm_up` returns what it came to, and should not raise. If it does,
        the state is `on_demand` -- the lazy path is still there -- and the
        error is the thread's, reported by `threading.excepthook`.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._readiness = SearchReadiness(SearchState.LOADING)
            self._thread = threading.Thread(
                target=self._run, args=(warm_up,), name="model-warm-up", daemon=True
            )
            self._thread.start()
            return self._thread

    def wait(self, timeout: float | None = None) -> bool:
        """Whether the warm-up has ended, waiting up to `timeout` for it."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _run(self, warm_up: Callable[[], SearchReadiness]) -> None:
        outcome = SearchReadiness(SearchState.ON_DEMAND)
        try:
            outcome = warm_up()
        finally:
            with self._lock:
                self._readiness = outcome


model_warm_up = ModelWarmUp()
"""The process's one warm-up, started by the API's lifespan."""

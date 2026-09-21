"""A small circuit breaker so an unhealthy provider fails fast instead of eating deadlines."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from jev_reactor.config import BreakerConfig
from jev_reactor.errors import ProviderError

BreakerState = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    """closed -> open after N consecutive transient failures -> half_open probe -> closed.

    * Only errors with ``counts_toward_breaker`` count (timeouts, 5xx, connection, 429).
      A rejected request or a malformed answer is a pack bug, not an outage.
    * ``ProviderAuthError`` opens the circuit immediately: retrying bad credentials is noise.
    * A ``retry_after`` from the provider (HTTP 429) lengthens the open period.
    """

    def __init__(
        self, config: BreakerConfig | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.config = config or BreakerConfig()
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0
        self._open_for = 0.0
        self._state: BreakerState = "closed"
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        # reading the state performs the open -> half_open transition
        if self._state == "open" and self._clock() - self._opened_at >= self._open_for:
            self._state = "half_open"
            self._probe_in_flight = False
        return self._state

    def allow(self) -> bool:
        """May a provider call be made right now? In half_open exactly one probe is allowed."""
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"
        self._probe_in_flight = False

    def record_failure(self, error: ProviderError | None = None) -> None:
        if error is not None and not error.counts_toward_breaker:
            # not an outage; release a half-open probe so it can be retried
            self._probe_in_flight = False
            return
        self._failures += 1
        immediate = error is not None and error.trips_immediately
        if (
            immediate
            or self._failures >= self.config.failure_threshold
            or self._state == "half_open"
        ):
            self._open(error)

    def _open(self, error: ProviderError | None) -> None:
        self._state = "open"
        self._opened_at = self._clock()
        wait = self.config.recovery_seconds
        if error is not None and error.retry_after:
            wait = max(wait, error.retry_after)
        self._open_for = wait
        self._probe_in_flight = False

    def release_probe(self) -> None:
        """A call ended without a verdict on provider health (superseded, cancelled, never sent).

        Frees a half-open probe slot without counting a success or a failure.
        """
        self._probe_in_flight = False

    def reset(self) -> None:
        self.record_success()

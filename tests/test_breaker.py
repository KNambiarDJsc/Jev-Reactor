from __future__ import annotations

from jev_reactor.breaker import CircuitBreaker
from jev_reactor.config import BreakerConfig
from jev_reactor.errors import (
    ProviderAuthError,
    ProviderRateLimitedError,
    ProviderRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def make(threshold: int = 3, recovery: float = 10.0) -> tuple[CircuitBreaker, Clock]:
    clock = Clock()
    return CircuitBreaker(
        BreakerConfig(failure_threshold=threshold, recovery_seconds=recovery), clock=clock
    ), clock


def test_opens_after_consecutive_transient_failures() -> None:
    cb, _ = make()
    for _ in range(2):
        cb.record_failure(ProviderUnavailableError("x"))
        assert cb.state == "closed" and cb.allow()
    cb.record_failure(ProviderTimeoutError("x"))
    assert cb.state == "open" and not cb.allow()


def test_a_success_resets_the_failure_count() -> None:
    cb, _ = make()
    cb.record_failure(ProviderUnavailableError("x"))
    cb.record_failure(ProviderUnavailableError("x"))
    cb.record_success()
    cb.record_failure(ProviderUnavailableError("x"))
    assert cb.state == "closed"


def test_half_open_admits_one_probe_then_closes_on_success() -> None:
    cb, clock = make(threshold=1, recovery=10)
    cb.record_failure(ProviderUnavailableError("x"))
    assert not cb.allow()
    clock.now += 10
    assert cb.state == "half_open"
    assert cb.allow() is True
    assert cb.allow() is False, "only one probe while the first is in flight"
    cb.record_success()
    assert cb.state == "closed" and cb.allow()


def test_a_failed_probe_reopens_the_circuit() -> None:
    cb, clock = make(threshold=1, recovery=10)
    cb.record_failure(ProviderUnavailableError("x"))
    clock.now += 10
    assert cb.allow()
    cb.record_failure(ProviderUnavailableError("x"))
    assert cb.state == "open"
    clock.now += 9
    assert not cb.allow()


def test_bad_credentials_open_the_circuit_immediately() -> None:
    cb, _ = make(threshold=99)
    cb.record_failure(ProviderAuthError("401", status=401))
    assert cb.state == "open"


def test_rejected_requests_and_malformed_answers_are_not_outages() -> None:
    cb, _ = make(threshold=1)
    cb.record_failure(ProviderRejectedError("422"))
    cb.record_failure(ProviderResponseError("bad body"))
    assert cb.state == "closed"


def test_retry_after_lengthens_the_open_period() -> None:
    cb, clock = make(threshold=1, recovery=2)
    cb.record_failure(ProviderRateLimitedError("429", retry_after=30))
    clock.now += 5
    assert cb.state == "open", "must honour the provider's requested wait, not just our default"
    clock.now += 25
    assert cb.state == "half_open"

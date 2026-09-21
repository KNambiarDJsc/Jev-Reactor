"""Reactor mechanics: deadlines, breaker, redaction, staleness, bounds, cancellation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import strict_config
from jev_reactor.config import BreakerConfig, ReactorConfig
from jev_reactor.errors import (
    ConfigError,
    PolicyError,
    ProviderAuthError,
    ProviderUnavailableError,
)
from jev_reactor.models import (
    Answer,
    DecisionEvent,
    DecisionResponse,
    QuestionSpec,
    ReactorEvent,
)
from jev_reactor.policy import RuleChainPolicy, RuleContext, decision, noul_strength
from jev_reactor.providers.mock import MockProvider
from jev_reactor.questions import QuestionPack
from jev_reactor.reactor import Reactor
from jev_reactor.sinks import JsonlSink, MemorySink

PACK = QuestionPack(
    id="mini",
    version="1",
    questions={"n": QuestionSpec(id="n", type="noul", instructions="Is `goal` done?")},
    state_paths=["goal"],
)


def mini_rule(ctx: RuleContext) -> Any:
    p = ctx.signals.noul("n")
    return decision("allow" if p > 0.5 else "skip", "mini", confidence=noul_strength(p))


def mini_policy(**kw: Any) -> RuleChainPolicy:
    return RuleChainPolicy([("mini", mini_rule)], policy_id="mini", **kw)


def make(provider: Any, **cfg: Any) -> Reactor:
    sinks = cfg.pop("sinks", ())
    policy = cfg.pop("policy", None) or mini_policy()
    clock = cfg.pop("clock", time.monotonic)
    return Reactor(
        provider,
        pack=PACK,
        policy=policy,
        config=strict_config(**cfg),
        sinks=sinks,
        clock=clock,
    )


def event(reactor: Reactor, **kw: Any) -> ReactorEvent:
    return reactor.new_event("t", {"goal": "ship it"}, goal="ship it", **kw)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ProbeProvider:
    """Records concurrency so tests can prove the in-flight bound."""

    def __init__(self, latency: float | Callable[[int], float] = 0.02, value: float = 0.9) -> None:
        self.latency = latency
        self.value = value
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.cancelled = 0

    async def evaluate(self, *, state: Any, questions: Any, timeout: float) -> DecisionResponse:
        self.calls += 1
        n = self.calls
        delay = self.latency(n) if callable(self.latency) else self.latency
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1
        return DecisionResponse(
            model="probe-1",
            latency_ms=delay * 1000,
            answers={"n": Answer(question_id="n", type="noul", noul=self.value)},
        )


# ---------------------------------------------------------------------------- one-shot


async def test_decide_returns_an_immutable_decision_tied_to_its_event() -> None:
    reactor = make(MockProvider({"n": 0.9}))
    ev = event(reactor)
    result = await reactor.decide(ev)
    assert result.action == "allow"
    assert (result.event_id, result.sequence, result.provider_status) == (ev.event_id, 1, "ok")
    assert result.rule == "mini"
    with pytest.raises(ValueError, match="frozen"):
        result.action = "block"  # type: ignore[misc]


async def test_one_request_carries_every_question() -> None:
    provider = MockProvider({"n": 0.9})
    reactor = make(provider)
    await reactor.decide(event(reactor))
    assert provider.call_count == 1 and provider.calls[0].question_ids == ["n"]


async def test_events_can_be_passed_as_plain_dicts() -> None:
    reactor = make(MockProvider({"n": 0.1}))
    result = await reactor.decide({"state": {"goal": "x"}, "event_type": "t"})
    assert result.action == "skip" and result.sequence == 1


async def test_missing_pack_or_policy_is_a_clear_error() -> None:
    reactor = Reactor(MockProvider({"n": 0.5}))
    with pytest.raises(ConfigError, match="question pack"):
        await reactor.decide({"state": {}})
    with pytest.raises(ConfigError, match="policy"):
        await Reactor(MockProvider(), pack=PACK).decide({"state": {}})


async def test_records_name_the_pack_fingerprint_policy_and_resolved_model() -> None:
    sink = MemorySink()
    reactor = make(MockProvider({"n": 0.9}, model="jev-1.13.0"), sinks=[sink])
    await reactor.decide(event(reactor))
    (rec,) = sink.records
    assert rec.question_pack_id == "mini" and rec.question_pack_fingerprint == PACK.fingerprint()
    assert rec.policy_id == "mini"
    assert rec.response is not None and rec.response.model == "jev-1.13.0"
    assert rec.decision_latency_ms is not None and rec.decision_latency_ms >= 0


# ---------------------------------------------------------------------------- hard rules


async def test_hard_rules_decide_without_calling_the_provider() -> None:
    def deny(ev: ReactorEvent) -> Any:
        return decision("block", "hard_rule") if ev.metadata.get("forbidden") else None

    provider = MockProvider({"n": 0.99})
    reactor = make(provider, policy=mini_policy(pre_rules=[("deny", deny)]))
    result = await reactor.decide(event(reactor, metadata={"forbidden": True}))
    assert (result.action, result.provider_status) == ("block", "not_called")
    assert provider.call_count == 0


async def test_hard_rules_run_even_when_the_state_builder_would_fail() -> None:
    pack = PACK.model_copy(update={"state_builder": lambda ev: 1 / 0})  # type: ignore[dict-item,arg-type,return-value]
    policy = mini_policy(pre_rules=[("deny", lambda ev: decision("block", "hard_rule"))])
    reactor = Reactor(MockProvider({"n": 0.9}), pack=pack, policy=policy, config=strict_config())
    assert (await reactor.decide(event(reactor))).action == "block"


# ---------------------------------------------------------------------------- redaction and persistence


async def test_secrets_are_redacted_before_the_provider_and_before_persistence() -> None:
    provider = MockProvider({"n": 0.9})
    sink = MemorySink()
    reactor = make(provider, sinks=[sink], persist_state="redacted")
    ev = reactor.new_event(
        "t",
        {"goal": "x", "api_key": "sk-abcdefghijklmnopqrstuvwx", "note": "token=abcdef123456 ok"},
        goal="x",
        proposed_action={"tool": "t", "arguments": {"password": "hunter2hunter2"}},
        metadata={"authorization": "Bearer abcdefgh12345678"},
    )
    await reactor.decide(ev)
    sent = json.dumps(provider.calls[0].state)
    stored = sink.records[0].model_dump_json()
    for leaked in (
        "sk-abcdefghijklmnopqrstuvwx",
        "abcdef123456",
        "hunter2hunter2",
        "abcdefgh12345678",
    ):
        assert leaked not in sent, leaked
        assert leaked not in stored, leaked
    assert "[REDACTED]" in sent


async def test_state_is_not_persisted_by_default_only_a_digest() -> None:
    sink = MemorySink()
    reactor = make(MockProvider({"n": 0.9}), sinks=[sink])
    await reactor.decide(event(reactor))
    rec = sink.records[0]
    assert rec.event.state == {} and rec.state_persisted is False
    assert rec.state_digest
    assert "ship it" not in rec.model_dump_json(), "goal text is content, not policy input"
    again = MemorySink()
    other = make(MockProvider({"n": 0.9}), sinks=[again])
    await other.decide(event(other))
    assert again.records[0].state_digest == rec.state_digest, "same state, same digest"


async def test_persist_state_redacted_keeps_the_redacted_state() -> None:
    sink = MemorySink()
    reactor = make(MockProvider({"n": 0.9}), sinks=[sink], persist_state="redacted")
    await reactor.decide(event(reactor))
    assert sink.records[0].event.state == {"goal": "ship it"} and sink.records[0].state_persisted


class RawProvider:
    async def evaluate(self, *, state: Any, questions: Any, timeout: float) -> DecisionResponse:
        return DecisionResponse(
            model="m",
            latency_ms=1,
            answers={"n": Answer(question_id="n", type="noul", noul=0.9)},
            raw={"echo": "sensitive raw payload"},
        )


async def test_raw_responses_are_dropped_unless_explicitly_kept() -> None:
    sink = MemorySink()
    await (lambda r: r.decide(event(r)))(make(RawProvider(), sinks=[sink]))
    assert sink.records[0].response is not None and sink.records[0].response.raw is None
    kept = MemorySink()
    r2 = make(RawProvider(), sinks=[kept], persist_raw_response=True)
    await r2.decide(event(r2))
    assert kept.records[0].response is not None and kept.records[0].response.raw is not None


async def test_the_api_key_from_the_environment_never_reaches_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "live-key-value-98765")
    provider = MockProvider({"n": 0.9})
    reactor = make(provider)
    ev = reactor.new_event("t", {"goal": "log says live-key-value-98765"}, goal="g")
    await reactor.decide(ev)
    assert "live-key-value-98765" not in json.dumps(provider.calls[0].state)


async def test_state_allowlist_sends_only_named_paths() -> None:
    provider = MockProvider({"n": 0.9})
    reactor = make(provider, state_allowlist=["goal"])
    await reactor.decide(reactor.new_event("t", {"goal": "g", "private": "x"}, goal="g"))
    assert provider.calls[0].state == {"goal": "g"}


async def test_oversized_state_is_not_sent() -> None:
    provider = MockProvider({"n": 0.9})
    reactor = make(provider, max_state_chars=256)
    big = reactor.new_event("t", {"goal": "x" * 5000}, goal="g")
    result = await reactor.decide(big)
    assert provider.call_count == 0 and result.provider_status == "not_called"
    assert "state_too_large" in result.reason_codes and result.action == "review"


async def test_state_builder_failures_fail_safe() -> None:
    pack = PACK.model_copy(update={"state_builder": lambda ev: 1 / 0})  # type: ignore[dict-item,arg-type,return-value]
    cfg = ReactorConfig()  # non-strict: degrade, do not raise
    reactor = Reactor(MockProvider({"n": 0.9}), pack=pack, policy=mini_policy(), config=cfg)
    result = await reactor.decide(event(reactor))
    assert result.action == "review" and "state_build_error" in result.reason_codes


# ---------------------------------------------------------------------------- deadlines and failures


async def test_the_deadline_is_enforced_by_the_reactor_and_bounds_latency() -> None:
    provider = MockProvider({"n": 0.9}, latency=0.5, respect_timeout=False)
    reactor = make(provider, deadline_seconds=0.05)
    started = time.perf_counter()
    result = await reactor.decide(event(reactor))
    assert time.perf_counter() - started < 0.3
    assert (result.action, result.provider_status) == ("review", "timeout")
    assert result.reason_codes == ["provider_timeout"]


async def test_the_deadline_is_handed_to_the_provider_as_its_timeout() -> None:
    provider = MockProvider({"n": 0.9})
    reactor = make(provider, deadline_seconds=0.7)
    await reactor.decide(event(reactor))
    assert 0 < provider.calls[0].timeout <= 0.7


async def test_provider_errors_fail_safe_with_the_configured_mode() -> None:
    provider = MockProvider({"n": 0.9}, raises=[ProviderUnavailableError("down")])
    reactor = make(provider, on_provider_failure="skip")
    result = await reactor.decide(event(reactor))
    assert (result.action, result.provider_status) == ("skip", "error")
    assert result.reason_codes == ["provider_error", "unavailable"]


async def test_unexpected_provider_exceptions_are_contained() -> None:
    reactor = make(MockProvider({"n": 0.9}, raises=[RuntimeError("boom")]))
    result = await reactor.decide(event(reactor))
    assert result.provider_status == "error" and result.action == "review"


async def test_a_malformed_response_is_an_error_not_a_decision() -> None:
    provider = MockProvider({})  # answers nothing
    reactor = make(provider)
    result = await reactor.decide(event(reactor))
    assert (result.provider_status, result.action) == ("error", "review")
    assert "malformed_response" in result.reason_codes
    for _ in range(10):
        await reactor.decide(event(reactor))
    assert reactor.breaker.state == "closed", "a bad answer is a pack bug, not an outage"


async def test_circuit_breaker_opens_then_recovers_through_a_probe() -> None:
    clock = FakeClock()
    provider = MockProvider(
        {"n": 0.9}, raises=[ProviderAuthError("401", status=401), None, None, None]
    )
    reactor = make(provider, clock=clock, breaker=BreakerConfig(recovery_seconds=10))
    assert (await reactor.decide(event(reactor))).provider_status == "error"
    assert reactor.breaker.state == "open"

    blocked = await reactor.decide(event(reactor))
    assert (blocked.provider_status, blocked.reason_codes) == (
        "circuit_open",
        ["provider_circuit_open"],
    )
    assert provider.call_count == 1, "an open circuit must not call the provider"

    clock.now += 11
    assert (await reactor.decide(event(reactor))).provider_status == "ok"
    assert reactor.breaker.state == "closed"
    assert (await reactor.decide(event(reactor))).action == "allow"


# ---------------------------------------------------------------------------- policy misbehaviour


async def test_a_raising_policy_degrades_to_review_without_leaking_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def bad(ctx: RuleContext) -> Any:
        raise RuntimeError("leak-marker-123")

    reactor = Reactor(
        MockProvider({"n": 0.9}),
        pack=PACK,
        policy=RuleChainPolicy([("bad", bad)], policy_id="bad"),
        config=ReactorConfig(),
    )
    with caplog.at_level(logging.DEBUG):
        result = await reactor.decide(event(reactor))
    assert (result.action, result.reason_codes) == ("review", ["policy_error"])
    assert result.metadata["error_type"] == "RuntimeError"
    assert "leak-marker-123" not in caplog.text and "RuntimeError" in caplog.text


class ForgetfulPolicy:
    """Returns a decision with an action that does not exist."""

    def decide(self, *, event: ReactorEvent, response: DecisionResponse) -> Any:
        return {"action": "call_tool", "reason_codes": ["x"]}


async def test_an_unknown_action_is_a_clear_policy_error() -> None:
    lenient = Reactor(
        MockProvider({"n": 0.9}), pack=PACK, policy=ForgetfulPolicy(), config=ReactorConfig()
    )
    result = await lenient.decide(event(lenient))
    assert (result.action, result.reason_codes) == ("review", ["policy_error"])

    strict = Reactor(
        MockProvider({"n": 0.9}), pack=PACK, policy=ForgetfulPolicy(), config=strict_config()
    )
    with pytest.raises(PolicyError, match="invalid decision"):
        await strict.decide(event(strict))


async def test_a_policy_that_returns_the_wrong_type_is_rejected() -> None:
    class Wrong:
        def decide(self, *, event: ReactorEvent, response: DecisionResponse) -> Any:
            return "allow"

    reactor = Reactor(MockProvider({"n": 0.9}), pack=PACK, policy=Wrong(), config=strict_config())
    with pytest.raises(PolicyError):
        await reactor.decide(event(reactor))


# ---------------------------------------------------------------------------- late results


async def test_a_late_result_is_recorded_but_never_applied() -> None:
    sink = MemorySink()
    provider = MockProvider({"n": 0.9}, latency=0.12, respect_timeout=False)
    reactor = make(provider, sinks=[sink], deadline_seconds=0.04, late_grace_seconds=1.0)
    started = time.perf_counter()
    result = await reactor.decide(event(reactor))
    assert time.perf_counter() - started < 0.1, "the caller must not wait for the late answer"
    assert (result.action, result.provider_status) == ("review", "timeout")

    await asyncio.sleep(0.25)
    statuses = [r.provider_status for r in sink.records]
    assert statuses == ["timeout", "late"]
    late = sink.records[1]
    assert late.policy_result.action == "review", "the decision that was actually returned"
    assert late.outcome is not None and late.outcome["late_counterfactual_action"] == "allow"
    await reactor.aclose()


async def test_aclose_cancels_pending_late_observers() -> None:
    provider = MockProvider({"n": 0.9}, latency=5, respect_timeout=False)
    reactor = make(provider, deadline_seconds=0.03, late_grace_seconds=10)
    await reactor.decide(event(reactor))
    assert reactor._background
    await reactor.aclose()
    assert not reactor._background


# ---------------------------------------------------------------------------- streaming


async def collect(agen: AsyncIterator[Any]) -> list[Any]:
    return [d async for d in agen]


async def test_results_keep_sequence_order_even_when_latencies_differ() -> None:
    latencies = {1: 0.12, 2: 0.01, 3: 0.06, 4: 0.01}
    provider = ProbeProvider(latency=lambda n: latencies[n])
    reactor = make(provider, stale="keep", max_in_flight=4, deadline_seconds=2)
    events = [event(reactor, metadata={"stream_id": f"s{i}"}) for i in range(4)]
    results = await collect(reactor.run(events))
    assert [r.sequence for r in results] == [1, 2, 3, 4]
    assert all(r.action == "allow" for r in results)


async def test_a_newer_event_supersedes_an_older_in_flight_decision() -> None:
    """Milestone 3 acceptance: a late response cannot change the action for a newer event."""
    provider = ProbeProvider(latency=lambda n: 0.4 if n == 1 else 0.01)
    reactor = make(provider, stale="cancel", deadline_seconds=2)
    e1, e2 = event(reactor), event(reactor)
    started = time.perf_counter()
    first, second = await collect(reactor.run([e1, e2]))
    assert time.perf_counter() - started < 0.3, "the superseded call must be cancelled, not awaited"
    assert (first.sequence, first.action, first.provider_status) == (1, "fallback", "stale")
    assert first.reason_codes == ["superseded"]
    assert (second.sequence, second.action, second.provider_status) == (2, "allow", "ok")
    assert provider.cancelled == 1


async def test_stale_events_on_different_streams_do_not_supersede_each_other() -> None:
    provider = ProbeProvider(latency=lambda n: 0.15 if n == 1 else 0.01)
    reactor = make(provider, stale="cancel", deadline_seconds=2)
    e1 = event(reactor, metadata={"stream_id": "a"})
    e2 = event(reactor, metadata={"stream_id": "b"})
    results = await collect(reactor.run([e1, e2]))
    assert [r.action for r in results] == ["allow", "allow"]


async def test_stale_keep_lets_every_decision_complete() -> None:
    provider = ProbeProvider(latency=lambda n: 0.1 if n == 1 else 0.01)
    reactor = make(provider, stale="keep", deadline_seconds=2)
    results = await collect(reactor.run([event(reactor), event(reactor)]))
    assert [r.provider_status for r in results] == ["ok", "ok"]


async def test_provider_calls_never_exceed_max_in_flight() -> None:
    provider = ProbeProvider(latency=0.02)
    reactor = make(provider, stale="keep", max_in_flight=3, deadline_seconds=5)
    events = [event(reactor) for _ in range(40)]
    results = await collect(reactor.run(events))
    assert len(results) == 40 and provider.max_active <= 3
    assert [r.sequence for r in results] == sorted(r.sequence for r in results)  # type: ignore[type-var]


async def test_a_slow_consumer_slows_the_source_instead_of_growing_memory() -> None:
    provider = ProbeProvider(latency=0.001)
    reactor = make(provider, stale="keep", max_in_flight=4, deadline_seconds=5)
    pulled = 0

    async def source() -> AsyncIterator[ReactorEvent]:
        nonlocal pulled
        for _ in range(200):
            pulled += 1
            yield event(reactor)

    consumed = 0
    stream = reactor.run(source())
    async for _ in stream:
        consumed += 1
        await asyncio.sleep(0.005)
        assert pulled <= consumed + 4 + 3, f"pulled {pulled} while only {consumed} were consumed"
        if consumed == 25:
            break
    await stream.aclose()  # type: ignore[attr-defined]


async def test_stopping_early_cancels_everything_cleanly() -> None:
    before = set(asyncio.all_tasks())
    provider = ProbeProvider(latency=0.2)
    reactor = make(provider, stale="keep", max_in_flight=4, deadline_seconds=5)

    async def endless() -> AsyncIterator[ReactorEvent]:
        while True:
            yield event(reactor)

    stream = reactor.run(endless())
    async for _ in stream:
        break
    await stream.aclose()  # type: ignore[attr-defined]
    await asyncio.sleep(0)
    leftover = {t for t in asyncio.all_tasks() if t not in before and not t.done()}
    assert not leftover, f"tasks still running: {leftover}"
    assert provider.active == 0


async def test_an_error_in_the_event_source_reaches_the_consumer() -> None:
    reactor = make(ProbeProvider(latency=0.001), stale="keep")

    async def flaky() -> AsyncIterator[ReactorEvent]:
        yield event(reactor)
        yield event(reactor)
        raise RuntimeError("source died")

    got: list[Any] = []

    async def drain() -> None:
        async for d in reactor.run(flaky()):
            got.append(d)

    with pytest.raises(RuntimeError, match="source died"):
        await drain()
    assert len(got) == 2


async def test_run_accepts_sync_iterables_of_dicts() -> None:
    reactor = make(MockProvider({"n": 0.9}), stale="keep")
    results = await collect(reactor.run([{"state": {"goal": "a"}}, {"state": {"goal": "b"}}]))
    assert [r.action for r in results] == ["allow", "allow"]


# ---------------------------------------------------------------------------- sinks and logging


class BoomSink:
    async def emit(self, record: DecisionEvent) -> None:
        raise RuntimeError("secret-state-marker")


async def test_a_failing_sink_never_breaks_a_decision_or_leaks_the_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    good = MemorySink()
    reactor = make(MockProvider({"n": 0.9}), sinks=[BoomSink(), good])
    with caplog.at_level(logging.DEBUG):
        result = await reactor.decide(event(reactor))
    assert result.action == "allow" and len(good.records) == 1
    assert "BoomSink failed: RuntimeError" in caplog.text
    assert "secret-state-marker" not in caplog.text


async def test_jsonl_sink_writes_one_parseable_line_per_decision(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "runs.jsonl"
    reactor = make(MockProvider({"n": 0.9}), sinks=[JsonlSink(path)])
    for _ in range(3):
        await reactor.decide(event(reactor))
    await reactor.aclose()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [DecisionEvent.from_jsonl(line) for line in lines]
    assert [p.event.sequence for p in parsed] == [1, 2, 3]


async def test_a_change_in_the_resolved_model_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    provider = MockProvider({"n": 0.9}, model="jev-1.13.0")
    reactor = make(provider)
    await reactor.decide(event(reactor))
    provider.model = "jev-1.14.0"
    with caplog.at_level(logging.WARNING, logger="jev_reactor"):
        await reactor.decide(event(reactor))
    assert "resolved model changed from jev-1.13.0 to jev-1.14.0" in caplog.text


async def test_stats_count_provider_calls_and_statuses() -> None:
    reactor = make(MockProvider({"n": 0.9}, raises=[None, ProviderUnavailableError("x")]))
    await reactor.decide(event(reactor))
    await reactor.decide(event(reactor))
    assert reactor.stats["provider_calls"] == 2
    assert reactor.stats["status_ok"] == 1 and reactor.stats["status_error"] == 1

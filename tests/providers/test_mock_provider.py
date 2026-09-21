from __future__ import annotations

import pytest

from jev_reactor.errors import ProviderTimeoutError, ProviderUnavailableError
from jev_reactor.models import QuestionSpec
from jev_reactor.providers import MockProvider
from jev_reactor.providers.mock import probabilities_for_confidence, score_probabilities

QUESTIONS = {
    "n": QuestionSpec(id="n", type="noul", instructions="Is it?"),
    "c": QuestionSpec(
        id="c", type="choice", instructions="Which?", criteria={"a": None, "b": None, "other": None}
    ),
    "s": QuestionSpec(id="s", type="score", instructions="How?", criteria=["low", "mid", "high"]),
}


async def test_builds_typed_answers_from_compact_values() -> None:
    p = MockProvider({"n": 0.9, "c": ("a", 0.8), "s": (1.4, 0.7)})
    r = await p.evaluate(state={"x": 1}, questions=QUESTIONS, timeout=1.0)
    assert r.answers["n"].noul == 0.9
    assert r.answers["c"].choice == "a" and r.answers["c"].confidence == 0.8
    assert r.answers["s"].score == 1.4 and r.answers["s"].legend["2"] == "high"
    assert p.call_count == 1 and p.calls[0].question_ids == ["n", "c", "s"]


def test_choice_probabilities_follow_the_documented_confidence_formula() -> None:
    probs = probabilities_for_confidence(["a", "b", "c"], "a", 0.7)
    n, p_max = 3, probs["a"]
    assert (n * p_max - 1) / (n - 1) == pytest.approx(0.7, abs=1e-3)
    assert sum(probs.values()) == pytest.approx(1.0, abs=1e-6)


def test_score_probabilities_reproduce_the_expectation() -> None:
    probs = score_probabilities(1.43, 3)
    assert sum(int(k) * v for k, v in probs.items()) == pytest.approx(1.43, abs=1e-3)


async def test_missing_answers_are_left_out_so_the_reactor_can_notice() -> None:
    r = await MockProvider({"n": 0.5}).evaluate(state="s", questions=QUESTIONS, timeout=1.0)
    assert set(r.answers) == {"n"}


async def test_rules_pick_answers_from_state() -> None:
    p = MockProvider(rules=[(lambda s: "refund" in s["t"], {"n": 0.99})], answers={"n": 0.01})
    hit = await p.evaluate(state={"t": "refund me"}, questions=QUESTIONS, timeout=1.0)
    miss = await p.evaluate(state={"t": "hello"}, questions=QUESTIONS, timeout=1.0)
    assert hit.answers["n"].noul == 0.99 and miss.answers["n"].noul == 0.01


async def test_scripted_failures_then_success() -> None:
    p = MockProvider({"n": 0.5}, raises=[ProviderUnavailableError("down"), None])
    with pytest.raises(ProviderUnavailableError):
        await p.evaluate(state="s", questions=QUESTIONS, timeout=1.0)
    assert (await p.evaluate(state="s", questions=QUESTIONS, timeout=1.0)).answers["n"].noul == 0.5


async def test_simulated_latency_respects_the_timeout() -> None:
    p = MockProvider({"n": 0.5}, latency=0.2)
    with pytest.raises(ProviderTimeoutError):
        await p.evaluate(state="s", questions=QUESTIONS, timeout=0.02)

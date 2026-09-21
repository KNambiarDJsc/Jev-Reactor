"""Policy tests. No network, no provider: responses are built by hand or from fixtures."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import TOOL_FIXTURES, TOOLS, event_from, reactor_for
from jev_reactor.config import ReactorConfig
from jev_reactor.errors import ConfigError
from jev_reactor.models import ACTIONS, DecisionResponse, ReactorEvent
from jev_reactor.packs import ToolLoop, ToolLoopPolicy, ToolLoopThresholds, ToolSpec
from jev_reactor.packs.tool_loop import tool_loop_questions
from jev_reactor.policy import Band, RuleChainPolicy, decision, noul_strength, weakest
from jev_reactor.providers.mock import build_answer, load_fixture

QUESTIONS = tool_loop_questions()


def make_event(
    tool: str = "search_invoices",
    args: dict[str, Any] | None = None,
    *,
    recent: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    proposed: dict[str, Any] | None = None,
) -> ReactorEvent:
    return ReactorEvent(
        event_id="evt_test",
        sequence=1,
        timestamp=datetime.now(UTC),
        event_type="tool_call_proposed",
        goal="Find the status of invoice INV-2041",
        state={"recent_calls": recent or []},
        proposed_action=proposed
        if proposed is not None
        else {"tool": tool, "arguments": args if args is not None else {"invoice_id": "INV-2041"}},
        metadata={"permissions": ["invoices:read"], **(metadata or {})},
    )


CALM = {
    "relevant": 0.95,
    "should_call": 0.95,
    "redundant": 0.05,
    "task_complete": 0.05,
    "needs_user_input": 0.05,
    "injection_suspected": 0.02,
    "next_action": ("call_tool", 0.9),
}


def response(**overrides: Any) -> DecisionResponse:
    values = {**CALM, **overrides}
    return DecisionResponse(
        model="jev-1.13.0",
        latency_ms=10,
        answers={qid: build_answer(QUESTIONS[qid], values[qid]) for qid in QUESTIONS},
    )


def decide(policy: ToolLoopPolicy, event: ReactorEvent, **overrides: Any) -> Any:
    return policy.decide(event=event, response=response(**overrides))


@pytest.fixture
def policy() -> ToolLoopPolicy:
    return ToolLoopPolicy(TOOLS)


# ---------------------------------------------------------------------------- toolkit


@pytest.mark.parametrize(
    ("p", "verdict"),
    [(0.00, "no"), (0.29, "no"), (0.30, "uncertain"), (0.50, "uncertain"), (0.70, "uncertain"),
     (0.71, "yes"), (1.00, "yes")],
)  # fmt: skip
def test_band_edges_are_strict_so_boundary_values_are_uncertain(p: float, verdict: str) -> None:
    assert Band(no_below=0.30, yes_above=0.70).classify(p) == verdict


def test_band_rejects_inverted_edges() -> None:
    with pytest.raises(ValueError, match="no_below"):
        Band(no_below=0.8, yes_above=0.2)


def test_noul_strength_is_distance_from_a_coin_flip() -> None:
    assert noul_strength(0.5) == 0
    assert noul_strength(1.0) == 1 == noul_strength(0.0)
    assert noul_strength(0.9) == pytest.approx(0.8)


def test_weakest_link_takes_the_minimum_and_ignores_missing() -> None:
    assert weakest(0.9, None, 0.4, 0.7) == 0.4
    assert weakest(None) is None


def test_a_rule_chain_never_defaults_to_allow() -> None:
    chain = RuleChainPolicy([("never", lambda ctx: None)], policy_id="t")
    result = chain.decide(event=make_event(), response=response())
    assert result.action == "review" and result.reason_codes == ["no_rule_matched"]


def test_rule_names_must_be_unique() -> None:
    with pytest.raises(ConfigError, match="unique"):
        RuleChainPolicy([("a", lambda c: None), ("a", lambda c: None)], policy_id="t")


def test_every_decision_names_the_rule_that_made_it(policy: ToolLoopPolicy) -> None:
    assert decide(policy, make_event()).rule == "allow_gate"
    assert (
        decide(policy, make_event(), redundant=0.95, next_action=("skip_tool", 0.9)).rule
        == "redundant"
    )


# ---------------------------------------------------------------------------- hard rules


def test_hard_rules_beat_a_confident_jev_allow(policy: ToolLoopPolicy) -> None:
    yes_everything = {"relevant": 0.99, "should_call": 0.99, "next_action": ("call_tool", 0.99)}
    cases = [
        (make_event("delete_everything"), "block", "tool_not_allowlisted"),
        (make_event(proposed={"arguments": {}}), "block", "malformed_proposal"),
        (make_event(proposed={"tool": "search_invoices", "arguments": "oops"}), "block", "malformed_proposal"),
        (make_event(metadata={"permissions": []}), "block", "permission_missing"),
        (make_event(metadata={"denied_tools": ["search_invoices"]}), "block", "user_denied"),
        (make_event("send_email", {"to": "a@b.c"}, metadata={"permissions": ["email:send"]}), "review", "irreversible_needs_approval"),
    ]  # fmt: skip
    for event, action, reason in cases:
        result = decide(policy, event, **yes_everything)
        assert (result.action, reason in result.reason_codes) == (action, True), (reason, result)


def test_the_hard_rules_also_run_before_the_provider_is_called(policy: ToolLoopPolicy) -> None:
    blocked = policy.pre_check(make_event("delete_everything"))
    assert blocked is not None and blocked.action == "block"
    assert policy.pre_check(make_event()) is None


def test_irreversible_actions_proceed_to_jev_only_with_explicit_approval(
    policy: ToolLoopPolicy,
) -> None:
    event = make_event(
        "send_email", {"to": "a@b.c"}, metadata={"permissions": ["email:send"], "approved": True}
    )
    assert policy.pre_check(event) is None
    assert decide(policy, event).action == "allow"


@pytest.mark.parametrize("approved", ["yes", 1, "true", None, False])
def test_only_a_literal_true_counts_as_approval(policy: ToolLoopPolicy, approved: Any) -> None:
    event = make_event(
        "send_email", {}, metadata={"permissions": ["email:send"], "approved": approved}
    )
    result = policy.pre_check(event)
    assert result is not None and result.action == "review"


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(499.99, None), (500, None), (500.01, "amount_exceeds_limit"), (-1, "amount_exceeds_limit"),
     (None, "amount_invalid"), ("abc", "amount_invalid"), (True, "amount_invalid"),
     (float("nan"), "amount_exceeds_limit"), ("120", None)],
)  # fmt: skip
def test_amount_limits_are_enforced_in_code(
    policy: ToolLoopPolicy, amount: Any, expected: str | None
) -> None:
    event = make_event(
        "refund_payment",
        {"amount": amount},
        metadata={"permissions": ["payments:refund"], "approved": True},
    )
    result = policy.pre_check(event)
    if expected is None:
        assert result is None
    else:
        assert result is not None and result.action == "block" and expected in result.reason_codes
    assert not math.isinf(500.0)


def test_exact_duplicates_of_idempotent_tools_are_skipped_without_jev(
    policy: ToolLoopPolicy,
) -> None:
    prior = {"tool": "search_invoices", "arguments": {"invoice_id": "INV-2041", "x": 1}, "ok": True}
    dup = make_event(args={"x": 1, "invoice_id": "INV-2041"}, recent=[prior])  # key order differs
    result = policy.pre_check(dup)
    assert result is not None and result.action == "skip"
    assert result.reason_codes == ["exact_duplicate_call"]


def test_duplicate_detection_is_conservative(policy: ToolLoopPolicy) -> None:
    prior = {"tool": "search_invoices", "arguments": {"invoice_id": "INV-2041"}, "ok": True}
    assert policy.pre_check(make_event(args={"invoice_id": "INV-9999"}, recent=[prior])) is None
    failed = {**prior, "ok": False}
    assert policy.pre_check(make_event(recent=[failed])) is None, "a failed call may be retried"
    # a non-idempotent tool is never skipped on textual equality
    same = {"tool": "get_invoice_status", "arguments": {"invoice_id": "INV-2041"}, "ok": True}
    assert (
        policy.pre_check(
            make_event("get_invoice_status", {"invoice_id": "INV-2041"}, recent=[same])
        )
        is None
    )


# ---------------------------------------------------------------------------- jev-driven rules


def test_confident_yes_on_everything_allows_and_reports_the_weakest_link(
    policy: ToolLoopPolicy,
) -> None:
    result = decide(policy, make_event(), next_action=("call_tool", 0.62))
    assert result.action == "allow"
    assert result.confidence == pytest.approx(0.62)
    assert set(result.metadata["signals"]) >= {"relevant", "should_call", "redundant"}


def test_task_complete_stops(policy: ToolLoopPolicy) -> None:
    result = decide(policy, make_event(), task_complete=0.97, next_action=("respond", 0.9))
    assert (result.action, result.target) == ("stop", "user")


def test_missing_information_asks_the_user(policy: ToolLoopPolicy) -> None:
    result = decide(policy, make_event(), needs_user_input=0.9, next_action=("ask_user", 0.9))
    assert (result.action, result.target, result.reason_codes) == (
        "review",
        "user",
        ["needs_clarification"],
    )


def test_low_relevance_with_ambiguous_usefulness_asks_the_user(policy: ToolLoopPolicy) -> None:
    result = decide(policy, make_event(), relevant=0.1, should_call=0.5)
    assert (result.action, result.target) == ("review", "user")


def test_irrelevant_and_useless_is_skipped(policy: ToolLoopPolicy) -> None:
    result = decide(
        policy, make_event(), relevant=0.05, should_call=0.05, next_action=("skip_tool", 0.9)
    )
    assert (result.action, result.reason_codes) == ("skip", ["not_relevant"])


def test_uncertain_band_goes_to_review_not_to_a_forced_yes_or_no(policy: ToolLoopPolicy) -> None:
    for name in ("relevant", "should_call", "redundant"):
        result = decide(policy, make_event(), **{name: 0.5})
        assert result.action == "review" and f"uncertain_{name}" in result.reason_codes


def test_disagreement_between_a_noul_and_the_choice_is_escalated(policy: ToolLoopPolicy) -> None:
    redundant_but_call = decide(
        policy, make_event(), redundant=0.95, next_action=("call_tool", 0.9)
    )
    assert "signals_disagree" in redundant_but_call.reason_codes
    complete_but_call = decide(
        policy, make_event(), task_complete=0.95, next_action=("call_tool", 0.9)
    )
    assert "signals_disagree" in complete_but_call.reason_codes
    choice_says_skip = decide(policy, make_event(), next_action=("skip_tool", 0.9))
    assert (
        choice_says_skip.action == "review" and "signals_disagree" in choice_says_skip.reason_codes
    )


def test_low_choice_confidence_routes_to_a_stronger_model(policy: ToolLoopPolicy) -> None:
    result = decide(policy, make_event(), next_action=("escalate", 0.3))
    assert (result.action, result.target) == ("route", "stronger_model")
    custom = ToolLoopPolicy(TOOLS, ToolLoopThresholds(escalation_target="human_queue"))
    assert decide(custom, make_event(), next_action=("escalate", 0.3)).target == "human_queue"


def test_injection_flag_never_lets_the_call_through(policy: ToolLoopPolicy) -> None:
    result = decide(policy, make_event(), injection_suspected=0.95)
    assert (result.action, result.reason_codes) == ("review", ["possible_prompt_injection"])


def test_uncertain_injection_only_blocks_riskier_tiers(policy: ToolLoopPolicy) -> None:
    read = decide(policy, make_event(), injection_suspected=0.4)
    assert read.action == "allow"
    write = decide(policy, make_event("write_note", {"note": "x"}), injection_suspected=0.4)
    assert write.action == "review" and write.reason_codes == ["injection_uncertain"]


def test_the_suppression_budget_stops_endless_skipping(policy: ToolLoopPolicy) -> None:
    skip = {"redundant": 0.95, "next_action": ("skip_tool", 0.9)}
    assert decide(policy, make_event(metadata={"consecutive_skips": 2}), **skip).action == "skip"
    exhausted = decide(policy, make_event(metadata={"consecutive_skips": 3}), **skip)
    assert exhausted.action == "review"
    assert exhausted.reason_codes[0] == "suppression_budget_exhausted"
    prior = {"tool": "search_invoices", "arguments": {"invoice_id": "INV-2041"}, "ok": True}
    dup = make_event(recent=[prior], metadata={"consecutive_skips": 3})
    forced = policy.pre_check(dup)
    assert forced is not None and forced.action == "review", "exact duplicates obey the budget too"


def test_strict_preset_routes_more_to_review_than_default() -> None:
    middling = {"relevant": 0.75, "should_call": 0.75}
    default = ToolLoopPolicy.from_preset("default", TOOLS)
    strict = ToolLoopPolicy.from_preset("strict", TOOLS)
    assert decide(default, make_event(), **middling).action == "allow"
    assert decide(strict, make_event(), **middling).action == "review"
    with pytest.raises(ValueError, match="unknown policy preset"):
        ToolLoopPolicy.from_preset("bogus")


def test_thresholds_can_be_tuned_per_signal() -> None:
    lenient = ToolLoopPolicy(TOOLS, ToolLoopThresholds(should_call=0.5, relevant=0.5))
    assert decide(lenient, make_event(), relevant=0.6, should_call=0.6).action == "allow"


# ---------------------------------------------------------------------------- failure modes


def test_unavailable_provider_fails_safe_by_risk_tier(policy: ToolLoopPolicy) -> None:
    cfg = ReactorConfig()
    cases = {
        "search_invoices": "fallback",
        "write_note": "review",
        "send_email": "block",
        "no_such_tool": "block",  # unknown means most conservative
    }
    for tool, mode in cases.items():
        result = policy.on_unavailable(make_event(tool), "timeout", cfg)
        assert result.action == mode and result.reason_codes == ["provider_timeout"], tool


def test_an_irreversible_tier_can_never_be_configured_to_auto_allow() -> None:
    with pytest.raises(ValueError, match="irreversible"):
        ReactorConfig(failure_by_risk={"irreversible": "allow"})


# ---------------------------------------------------------------------------- scenarios


@pytest.mark.parametrize("path", TOOL_FIXTURES, ids=[p.stem for p in TOOL_FIXTURES])
async def test_recorded_scenarios_produce_their_expected_actions(path: Any, loop: ToolLoop) -> None:
    fx = load_fixture(path)
    reactor, provider = reactor_for(path, loop)
    result = await reactor.decide(event_from(reactor, fx.event))
    assert result.action == fx.expected["action"], result
    for reason in fx.expected["reason_codes"]:
        assert reason in result.reason_codes
    assert (provider.call_count == 1) is fx.expected["provider_called"]


async def test_the_four_headline_scenarios_return_four_different_actions(loop: ToolLoop) -> None:
    actions = {}
    for name in ("safe_tool_call", "redundant_tool_call", "ambiguous_tool_call", "complete_task"):
        path = next(p for p in TOOL_FIXTURES if p.stem == name)
        reactor, _ = reactor_for(path, loop)
        actions[name] = (await reactor.decide(event_from(reactor, load_fixture(path).event))).action
    assert actions == {
        "safe_tool_call": "allow",
        "redundant_tool_call": "skip",
        "ambiguous_tool_call": "review",
        "complete_task": "stop",
    }


# ---------------------------------------------------------------------------- properties

prob = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
choices = st.sampled_from(["call_tool", "skip_tool", "ask_user", "respond", "escalate"])
confidence = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@settings(max_examples=300, deadline=None)
@given(
    relevant=prob, should_call=prob, redundant=prob, task_complete=prob,
    needs_user_input=prob, injection_suspected=prob, choice=choices, conf=confidence,
)  # fmt: skip
def test_property_allow_only_comes_from_one_specific_combination(
    relevant: float, should_call: float, redundant: float, task_complete: float,
    needs_user_input: float, injection_suspected: float, choice: str, conf: float,
) -> None:  # fmt: skip
    """Whatever Jev says, the policy returns a valid action, and ``allow`` is never an accident."""
    policy = ToolLoopPolicy(TOOLS)
    t = ToolLoopThresholds()
    result = decide(
        policy,
        make_event(),
        relevant=round(relevant, 2), should_call=round(should_call, 2), redundant=round(redundant, 2),
        task_complete=round(task_complete, 2), needs_user_input=round(needs_user_input, 2),
        injection_suspected=round(injection_suspected, 2), next_action=(choice, round(conf, 2)),
    )  # fmt: skip
    assert result.action in ACTIONS and result.reason_codes
    if result.action == "allow":
        s = result.metadata["signals"]
        assert s["relevant"] > t.relevant and s["should_call"] > t.should_call
        assert s["redundant"] < t.no_below and s["task_complete"] < t.no_below
        assert (
            s["needs_user_input"] < t.no_below and s["injection_suspected"] <= t.injection_suspected
        )
        assert choice == "call_tool" and round(conf, 2) >= t.minimum_choice_confidence


@settings(max_examples=200, deadline=None)
@given(tool=st.text(max_size=12), perms=st.lists(st.text(max_size=8), max_size=3))
def test_property_an_unlisted_tool_can_never_be_allowed(tool: str, perms: list[str]) -> None:
    policy = ToolLoopPolicy(TOOLS)
    event = make_event(tool or "x", metadata={"permissions": perms, "approved": True})
    if tool in {t.name for t in TOOLS}:
        return
    result = decide(policy, event)
    assert result.action == "block"


def test_decision_helper_validates_actions() -> None:
    with pytest.raises(ValueError, match="action"):
        decision("call_tool", "x")  # type: ignore[arg-type]


def test_tool_spec_is_frozen_config_not_event_data() -> None:
    spec = ToolSpec(name="x")
    assert spec.risk == "write", "unknown risk defaults to the reviewable tier"
    with pytest.raises(ValueError, match="frozen"):
        spec.risk = "read"  # type: ignore[misc]


def test_loop_facade_builds_matching_pack_and_policy() -> None:
    loop = ToolLoop(TOOLS)
    assert loop.pack.id == "tool-loop" and loop.policy.policy_id == "tool-loop/default"
    assert set(loop.pack.questions) == set(QUESTIONS)

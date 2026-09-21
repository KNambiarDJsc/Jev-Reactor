"""Deterministic policy toolkit. Ordinary Python: unit-testable without a provider or network.

Three ideas from TypeSafe's docs shape this module:

* **Two-sided Noul bands.** A single threshold sends 0.49 and 0.51 to opposite actions even
  though both express substantial uncertainty. A :class:`Band` has a confident-no side, a
  confident-yes side, and an ``uncertain`` middle that policies route to review.
* **Confidence is a second axis.** The answer says *what*; confidence says *whether to act*.
  Noul has no confidence, so its distance from a coin flip is used, and a decision's
  confidence is the weakest link across the answers its winning rule consulted.
* **No structural invariants.** Separate questions are not guaranteed to agree (a Noul and its
  negation need not sum to 1; a Choice and a Noul answer different questions). Policies must
  detect disagreement and escalate, never assume consistency.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from jev_reactor.config import ReactorConfig
from jev_reactor.errors import ConfigError, PolicyError
from jev_reactor.models import (
    ActionDecision,
    ActionName,
    Answer,
    DecisionResponse,
    FailureMode,
    ProviderStatus,
    ReactorEvent,
)

Verdict = Literal["yes", "no", "uncertain"]


class Band(BaseModel):
    """``p < no_below`` is a confident no, ``p > yes_above`` a confident yes, else uncertain.

    Both edges are strict, so a value exactly on a boundary is *uncertain*. Wire values are
    rounded to two decimals, which makes boundary hits common; treating them as uncertain
    errs toward review. The 0.30 / 0.70 defaults are the illustrative band from TypeSafe's
    self-consistency cookbook and are **not calibrated**: set yours from labelled traces.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    no_below: float = 0.30
    yes_above: float = 0.70

    @model_validator(mode="after")
    def _ordered(self) -> Band:
        if not 0.0 <= self.no_below <= self.yes_above <= 1.0:
            raise ValueError("band needs 0 <= no_below <= yes_above <= 1")
        return self

    def classify(self, p: float) -> Verdict:
        if p > self.yes_above:
            return "yes"
        if p < self.no_below:
            return "no"
        return "uncertain"


def noul_strength(p: float) -> float:
    """How far a Noul is from a coin flip, on 0..1. Not a calibrated confidence."""
    return max(p, 1.0 - p) * 2.0 - 1.0


def weakest(*strengths: float | None) -> float | None:
    """The least certain judgement behind a decision (weakest-link confidence)."""
    known = [s for s in strengths if s is not None]
    return round(min(known), 4) if known else None


class Signals:
    """Typed, checked access to a response's answers for policy code."""

    def __init__(self, response: DecisionResponse) -> None:
        self.response = response

    def _get(self, qid: str, kind: str) -> Answer:
        ans = self.response.answers.get(qid)
        if ans is None:
            raise PolicyError(f"policy needs an answer for {qid!r} but the response has none")
        if ans.type != kind:
            raise PolicyError(f"policy expected {qid!r} to be a {kind} answer, got {ans.type}")
        return ans

    def noul(self, qid: str) -> float:
        value = self._get(qid, "noul").noul
        assert value is not None
        return value

    def choice(self, qid: str) -> Answer:
        return self._get(qid, "choice")

    def score(self, qid: str) -> Answer:
        return self._get(qid, "score")

    def has(self, qid: str) -> bool:
        return qid in self.response.answers


@dataclass(frozen=True)
class RuleContext:
    event: ReactorEvent
    signals: Signals

    @property
    def response(self) -> DecisionResponse:
        return self.signals.response


Rule = Callable[[RuleContext], ActionDecision | None]
PreRule = Callable[[ReactorEvent], ActionDecision | None]


@runtime_checkable
class Policy(Protocol):
    """The only required method. Optional hooks: ``pre_check``, ``on_unavailable``,
    ``risk_tier`` and a ``policy_id`` attribute (see :class:`RuleChainPolicy`)."""

    def decide(self, *, event: ReactorEvent, response: DecisionResponse) -> ActionDecision: ...


def decision(
    action: ActionName,
    *reasons: str,
    confidence: float | None = None,
    target: str | None = None,
    risk_score: float | None = None,
    **metadata: Any,
) -> ActionDecision:
    return ActionDecision(
        action=action,
        reason_codes=list(reasons),
        confidence=confidence,
        target=target,
        risk_score=risk_score,
        metadata=metadata,
    )


def failure_decision(
    mode: FailureMode, status: ProviderStatus, *extra_reasons: str, **metadata: Any
) -> ActionDecision:
    """What to return when Jev could not be consulted: the configured fail-safe mode."""
    return decision(mode, f"provider_{status}", *extra_reasons, **metadata)


class RuleChainPolicy:
    """First-match-wins rules over a response, with hard rules that run before Jev.

    * ``pre_rules`` see only the event. They run *before* the provider is called, so a
      denied call never reaches Jev, and again inside ``decide`` so a replay re-applies them.
    * ``rules`` run in order; the first that returns a decision wins and is stamped with its
      name, so every decision says which rule made it.
    * If nothing matches the result is ``review``. A chain can never silently default to allow.
    """

    def __init__(
        self,
        rules: Sequence[tuple[str, Rule]],
        *,
        policy_id: str,
        pre_rules: Sequence[tuple[str, PreRule]] = (),
        risk_tier: Callable[[ReactorEvent], str | None] | None = None,
    ) -> None:
        names = [n for n, _ in (*pre_rules, *rules)]
        if len(names) != len(set(names)):
            raise ConfigError("rule names must be unique so decisions can be attributed")
        self.policy_id = policy_id
        self._pre_rules = list(pre_rules)
        self._rules = list(rules)
        self._risk_tier = risk_tier

    def risk_tier(self, event: ReactorEvent) -> str | None:
        return self._risk_tier(event) if self._risk_tier else None

    def pre_check(self, event: ReactorEvent) -> ActionDecision | None:
        for name, rule in self._pre_rules:
            result = rule(event)
            if result is not None:
                return result.model_copy(update={"rule": name})
        return None

    def decide(self, *, event: ReactorEvent, response: DecisionResponse) -> ActionDecision:
        pre = self.pre_check(event)
        if pre is not None:
            return pre
        ctx = RuleContext(event=event, signals=Signals(response))
        for name, rule in self._rules:
            result = rule(ctx)
            if result is not None:
                return result.model_copy(update={"rule": name})
        return decision("review", "no_rule_matched", target="operator").model_copy(
            update={"rule": "default_review"}
        )

    def on_unavailable(
        self, event: ReactorEvent, status: ProviderStatus, config: ReactorConfig
    ) -> ActionDecision:
        tier = self.risk_tier(event)
        mode = config.failure_by_risk.get(tier or "", config.on_provider_failure)
        return failure_decision(mode, status, **({"risk_tier": tier} if tier else {})).model_copy(
            update={"rule": "provider_unavailable"}
        )

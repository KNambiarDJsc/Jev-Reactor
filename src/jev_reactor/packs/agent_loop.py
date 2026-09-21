"""Agent-loop pack: turn-level decisions (stop, ask the user, or pick a model tier).

Where the tool-loop pack gates one tool call, this pack answers the turn-level questions from
the brief: is the task complete, does the user need to be asked, and which kind of model
should write the next step.

Model routing borrows one lesson from the community's jev-gate router: when confidence in the
cheapest tier is low, step up **one** tier, not straight to the top. Falling back to the most
expensive model on every uncertain turn eats most of the saving.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from jev_reactor.models import ActionDecision, ActionName, QuestionSpec, ReactorEvent
from jev_reactor.policy import (
    Band,
    Rule,
    RuleChainPolicy,
    RuleContext,
    decision,
    noul_strength,
    weakest,
)
from jev_reactor.questions import QuestionPack

PACK_ID = "agent-loop"
PACK_VERSION = "1.0"

TIERS = ("fast", "standard", "frontier")

STATE_PATHS = ["goal", "agent_context", "progress", "current_tier"]


def agent_loop_questions() -> dict[str, QuestionSpec]:
    qs = [
        QuestionSpec(
            id="goal_satisfied",
            type="noul",
            instructions="Does `progress` already satisfy `goal`?",
            criteria={
                "true": "Everything `goal` asks for is already done or answered in `progress`.",
                "false": "Something `goal` asks for is still not done.",
            },
        ),
        QuestionSpec(
            id="needs_user_input",
            type="noul",
            instructions="Is a detail needed to continue missing from both `goal` and `progress`?",
            criteria={
                "true": "A needed detail is absent and only the user can supply it.",
                "false": "Every needed detail is present.",
            },
        ),
        QuestionSpec(
            id="model_tier",
            type="choice",
            instructions="Which kind of model is needed to write the next step of `goal`, "
            "given `progress`?",
            criteria={
                "fast": "A short routine step: a lookup, a formatting change, or a simple edit.",
                "standard": "A normal step that needs careful reading but no deep reasoning.",
                "frontier": "A hard step: design, debugging across several parts, or reasoning "
                "over many facts.",
                "unclear": "It is not possible to tell which kind of model is needed.",
            },
        ),
    ]
    return {q.id: q for q in qs}


def make_state_builder() -> Any:
    def build(event: ReactorEvent) -> dict[str, Any]:
        state = {
            "goal": event.goal or event.state.get("goal"),
            "agent_context": event.state.get("agent_context"),
            "progress": event.state.get("progress"),
            "current_tier": event.state.get("current_tier"),
        }
        return {k: v for k, v in state.items() if v is not None and v != ""}

    return build


def agent_loop_pack() -> QuestionPack:
    return QuestionPack(
        id=PACK_ID,
        version=PACK_VERSION,
        description="Turn-level decisions: stop, ask the user, or route to a model tier.",
        questions=agent_loop_questions(),
        state_paths=list(STATE_PATHS),
        state_builder=make_state_builder(),
        thresholds=AgentLoopThresholds().model_dump(),
    )


class AgentLoopThresholds(BaseModel):
    """Initial defaults, not calibrated values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal_satisfied: Band = Band(no_below=0.30, yes_above=0.85)
    needs_user_input: Band = Band(no_below=0.30, yes_above=0.70)
    #: below this Choice confidence the router steps up one tier
    minimum_tier_confidence: float = 0.50
    #: tier used when the Choice abstains (``unclear``)
    unclear_tier: str = "standard"


class AgentLoopPolicy(RuleChainPolicy):
    def __init__(
        self,
        thresholds: AgentLoopThresholds | None = None,
        *,
        policy_id: str = "agent-loop/default",
    ) -> None:
        self.t = thresholds or AgentLoopThresholds()
        super().__init__(
            rules=self._build_rules(),
            policy_id=policy_id,
            risk_tier=lambda _e: "write",  # failure means "ask a person"
        )

    def _build_rules(self) -> list[tuple[str, Rule]]:
        t = self.t

        def stop(ctx: RuleContext) -> ActionDecision | None:
            p = ctx.signals.noul("goal_satisfied")
            if t.goal_satisfied.classify(p) == "yes":
                return decision(
                    "stop",
                    "goal_satisfied",
                    target="user",
                    confidence=weakest(noul_strength(p)),
                    signals={"goal_satisfied": p},
                )
            return None

        def ask(ctx: RuleContext) -> ActionDecision | None:
            p = ctx.signals.noul("needs_user_input")
            if t.needs_user_input.classify(p) == "yes":
                return decision(
                    "review",
                    "needs_clarification",
                    target="user",
                    confidence=weakest(noul_strength(p)),
                    signals={"needs_user_input": p},
                )
            return None

        def route(ctx: RuleContext) -> ActionDecision | None:
            ans = ctx.signals.choice("model_tier")
            current = ctx.event.state.get("current_tier")
            conf = ans.confidence if ans.confidence is not None else 0.0
            tier = ans.choice
            reasons = [f"tier_{tier}"]
            if tier == "unclear":
                tier = t.unclear_tier
                reasons = ["tier_unclear"]
            elif conf < t.minimum_tier_confidence and tier in TIERS:
                # step up one tier, never straight to the top
                tier = TIERS[min(TIERS.index(tier) + 1, len(TIERS) - 1)]
                reasons.append("low_confidence_step_up")
            action: ActionName = "allow" if tier == current else "route"
            return decision(action, *reasons, target=tier, confidence=conf)

        return [("stop", stop), ("ask_user", ask), ("route_model", route)]

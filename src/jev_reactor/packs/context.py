"""Context-retention pack: keep, compact, or discard one context item at a time.

Lessons taken from community context tools built on Jev (jev-gate ``keep``, pi-jev-context):

* **When in doubt, keep it.** Discarding needs several independent signals to agree; anything
  uncertain is retained. The one place this library's default action is ``allow`` is here,
  because keeping context is the non-destructive direction.
* **Code guarantees what must survive.** Pins (recent turns, system messages, anything that
  looks like a failure or stack trace) are decided in code *before* Jev is asked, so they cost
  nothing and can never be discarded by a wrong probability.
* **Jev judges, it does not rewrite.** It cannot generate text, so ``compact`` only says "a
  shorter representation is enough". The host summarises, or drops the item verbatim.
* **One item per request.** An independent benchmark found that packing many rows into one
  state degrades ranking; this pack asks about a single item, and :func:`classify_items` runs
  items concurrently under the Reactor's bounds.

Actions map to dispositions: ``allow`` = retain, ``compact`` = compact, ``skip`` = discard,
with the disposition also in ``target``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jev_reactor.models import ActionDecision, QuestionSpec, ReactorEvent
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
from jev_reactor.reactor import Reactor

PACK_ID = "context-retention"
PACK_VERSION = "1.0"

STATE_PATHS = [
    "goal",
    "agent_context",
    "item.id",
    "item.role",
    "item.text",
    "item.age_turns",
    "item.pinned",
]

_FAILURE_MARKERS = re.compile(
    r"(?i)\b(traceback|exception|error|failed|failure|fatal|panic|segfault|denied|timeout|"
    r"timed out|assertionerror|stack trace)\b"
)


def _noul(qid: str, instructions: str, yes: str, no: str) -> QuestionSpec:
    return QuestionSpec(
        id=qid, type="noul", instructions=instructions, criteria={"true": yes, "false": no}
    )


def context_questions() -> dict[str, QuestionSpec]:
    qs = [
        _noul(
            "relevant_to_goal",
            "Is `item.text` relevant to `goal`?",
            "`item.text` is about something `goal` still involves.",
            "`item.text` is about something unrelated to `goal`.",
        ),
        _noul(
            "states_preference",
            "Does `item.text` state a preference or standing instruction from the user "
            "that should keep applying?",
            "The user says how they want things done from now on.",
            "The text states no lasting preference or instruction.",
        ),
        _noul(
            "open_commitment",
            "Does `item.text` contain a promise, task, or question that is still unresolved?",
            "Something was promised or asked and has not been done or answered yet.",
            "Nothing in the text is left open.",
        ),
        _noul(
            "needed_evidence",
            "Does `item.text` contain a fact, value, or result that a later answer about "
            "`goal` may need to quote exactly?",
            "The text holds specific values, names, or results that must stay exact.",
            "The text holds nothing that would need to be quoted exactly.",
        ),
        _noul(
            "sensitive",
            "Does `item.text` contain personal, financial, or credential information?",
            "The text includes such information.",
            "The text includes no such information.",
        ),
        _noul(
            "safe_to_summarize",
            "Would a one-sentence summary of `item.text` keep everything `goal` still "
            "needs from it?",
            "A short summary loses nothing that `goal` needs.",
            "A summary would lose detail that `goal` needs.",
        ),
        _noul(
            "safe_to_discard",
            "Could `item.text` be deleted with no effect on `goal` or on anything the "
            "user asked for?",
            "Deleting it changes nothing that matters.",
            "Deleting it would lose something that matters.",
        ),
        QuestionSpec(
            id="preservation_need",
            type="score",
            instructions="How much of the exact content of `item.text` does `goal` still need?",
            criteria=[
                "None of it: the item is unrelated to `goal`, out of date, or fully replaced "
                "by later items.",
                "Only the gist: the outcome matters, but exact wording, numbers, and names do not.",
                "The exact content: specific values, names, wording, instructions, or "
                "commitments must survive unchanged.",
            ],
        ),
    ]
    return {q.id: q for q in qs}


def make_state_builder(*, max_item_chars: int = 1500) -> Any:
    def build(event: ReactorEvent) -> dict[str, Any]:
        item = dict(event.state.get("item") or {})
        text = item.get("text")
        if isinstance(text, str) and len(text) > max_item_chars:
            item["text"] = (
                text[:max_item_chars] + f"…[truncated {len(text) - max_item_chars} chars]"
            )
        state = {
            "goal": event.goal or event.state.get("goal"),
            "agent_context": event.state.get("agent_context"),
            "item": item,
        }
        return {k: v for k, v in state.items() if v is not None and v != ""}

    return build


def context_pack() -> QuestionPack:
    return QuestionPack(
        id=PACK_ID,
        version=PACK_VERSION,
        description=(
            "Decide whether one context item should be retained, compacted, or discarded. "
            "Judgments only: Jev never rewrites text."
        ),
        questions=context_questions(),
        state_paths=list(STATE_PATHS),
        state_builder=make_state_builder(),
        thresholds=ContextThresholds().model_dump(),
    )


class ContextThresholds(BaseModel):
    """Initial defaults, not calibrated values. Set yours from labelled traces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: any of these being a confident yes retains the item
    retain_signal: Band = Band(no_below=0.30, yes_above=0.70)
    discard_signal: Band = Band(no_below=0.30, yes_above=0.70)
    summarize_signal: Band = Band(no_below=0.30, yes_above=0.70)
    #: preservation_need at or above this means exact content is needed (retain)
    retain_score_at: float = 1.5
    #: at or below this the item is not needed (discard candidate)
    discard_score_at: float = 0.5
    minimum_score_confidence: float = 0.60
    #: the newest N turns are pinned in code and never sent to Jev
    keep_recent_turns: int = Field(default=4, ge=0)


class ContextPolicy(RuleChainPolicy):
    def __init__(
        self, thresholds: ContextThresholds | None = None, *, policy_id: str = "context/default"
    ) -> None:
        self.t = thresholds or ContextThresholds()
        super().__init__(
            rules=self._build_rules(),
            pre_rules=self._build_pre_rules(),
            policy_id=policy_id,
            risk_tier=lambda _e: "read",  # failure means "keep everything"
        )

    # -- pins: decided in code, before Jev, retain-only --------------------------------

    def _build_pre_rules(self) -> list[tuple[str, Any]]:
        keep = self.t.keep_recent_turns

        def item(event: ReactorEvent) -> Mapping[str, Any]:
            raw = event.state.get("item")
            return raw if isinstance(raw, Mapping) else {}

        def retain(reason: str) -> ActionDecision:
            return decision("allow", reason, target="retain", pinned=True)

        def malformed(event: ReactorEvent) -> ActionDecision | None:
            if not isinstance(item(event).get("text"), str):
                return retain("no_item_text")  # nothing to judge: keep it
            return None

        def pinned_flag(event: ReactorEvent) -> ActionDecision | None:
            return retain("pinned") if item(event).get("pinned") is True else None

        def system(event: ReactorEvent) -> ActionDecision | None:
            return retain("system_message") if item(event).get("role") == "system" else None

        def recent(event: ReactorEvent) -> ActionDecision | None:
            age = item(event).get("age_turns")
            if isinstance(age, int) and not isinstance(age, bool) and age < keep:
                return retain("recent_turn")
            return None

        def failure_marker(event: ReactorEvent) -> ActionDecision | None:
            text = item(event).get("text")
            if isinstance(text, str) and _FAILURE_MARKERS.search(text):
                return retain("failure_marker")
            return None

        return [
            ("no_item_text", malformed),
            ("pinned_flag", pinned_flag),
            ("system_message", system),
            ("recent_turn", recent),
            ("failure_marker", failure_marker),
        ]

    # -- Jev-driven rules -------------------------------------------------------------

    def _build_rules(self) -> list[tuple[str, Rule]]:
        t = self.t

        def verdicts(ctx: RuleContext) -> tuple[dict[str, str], dict[str, float]]:
            s = ctx.signals
            names = {
                "states_preference": t.retain_signal,
                "open_commitment": t.retain_signal,
                "needed_evidence": t.retain_signal,
                "relevant_to_goal": t.retain_signal,
                "safe_to_discard": t.discard_signal,
                "safe_to_summarize": t.summarize_signal,
            }
            p = {n: s.noul(n) for n in [*names, "sensitive"]}
            return {n: band.classify(p[n]) for n, band in names.items()}, p

        def meta(p: dict[str, float]) -> dict[str, Any]:
            return {"signals": p, "sensitive": p["sensitive"] > 0.5}

        def retain_signal(ctx: RuleContext) -> ActionDecision | None:
            v, p = verdicts(ctx)
            yes = [
                n
                for n in ("states_preference", "open_commitment", "needed_evidence")
                if v[n] == "yes"
            ]
            if yes:
                return decision(
                    "allow",
                    *yes,
                    target="retain",
                    confidence=weakest(*(noul_strength(p[n]) for n in yes)),
                    **meta(p),
                )
            return None

        def exact_content(ctx: RuleContext) -> ActionDecision | None:
            _, p = verdicts(ctx)
            score = ctx.signals.score("preservation_need")
            if score.score is not None and score.score >= t.retain_score_at:
                return decision(
                    "allow",
                    "needs_exact_content",
                    target="retain",
                    confidence=score.confidence,
                    **meta(p),
                )
            return None

        def uncertain_keep(ctx: RuleContext) -> ActionDecision | None:
            v, p = verdicts(ctx)
            unsure = [
                n
                for n in ("states_preference", "open_commitment", "needed_evidence")
                if v[n] == "uncertain"
            ]
            if unsure:
                return decision("allow", "uncertain_keep", *unsure, target="retain", **meta(p))
            return None

        def discard(ctx: RuleContext) -> ActionDecision | None:
            v, p = verdicts(ctx)
            score = ctx.signals.score("preservation_need")
            if (
                v["safe_to_discard"] == "yes"
                and v["relevant_to_goal"] == "no"
                and score.score is not None
                and score.score <= t.discard_score_at
                and (score.confidence or 0.0) >= t.minimum_score_confidence
            ):
                return decision(
                    "skip",
                    "safe_to_discard",
                    target="discard",
                    confidence=weakest(
                        noul_strength(p["safe_to_discard"]),
                        noul_strength(p["relevant_to_goal"]),
                        score.confidence,
                    ),
                    **meta(p),
                )
            return None

        def compact(ctx: RuleContext) -> ActionDecision | None:
            v, p = verdicts(ctx)
            score = ctx.signals.score("preservation_need")
            if (
                v["safe_to_summarize"] == "yes"
                and score.score is not None
                and score.score < t.retain_score_at
                and (score.confidence or 0.0) >= t.minimum_score_confidence
            ):
                return decision(
                    "compact",
                    "safe_to_summarize",
                    target="compact",
                    confidence=weakest(noul_strength(p["safe_to_summarize"]), score.confidence),
                    **meta(p),
                )
            return None

        def default_keep(ctx: RuleContext) -> ActionDecision | None:
            _, p = verdicts(ctx)
            return decision("allow", "default_keep", target="retain", **meta(p))

        return [
            ("retain_signal", retain_signal),
            ("exact_content", exact_content),
            ("uncertain_keep", uncertain_keep),
            ("discard", discard),
            ("compact", compact),
            ("default_keep", default_keep),
        ]


# ---------------------------------------------------------------------------- helpers


@dataclass
class CompactionPlan:
    """What to keep, shorten, or drop. Nothing is deleted here; the host applies it.

    Dropped items should stay recoverable (store the originals) because a wrong discard is
    only cheap if it is reversible.
    """

    retain: list[str] = field(default_factory=list)
    compact: list[str] = field(default_factory=list)
    discard: list[str] = field(default_factory=list)
    decisions: dict[str, ActionDecision] = field(default_factory=dict)

    @property
    def compaction_rate(self) -> float:
        total = len(self.decisions)
        return (len(self.compact) + len(self.discard)) / total if total else 0.0


async def classify_items(
    reactor: Reactor,
    items: Sequence[Mapping[str, Any]],
    *,
    goal: str,
    agent_context: str | None = None,
    pack: QuestionPack | None = None,
    policy: ContextPolicy | None = None,
) -> CompactionPlan:
    """Classify each item with its own request, concurrently and within the Reactor's bounds.

    Each item gets its own stream id so items never supersede one another.
    """
    pack = pack or context_pack()
    policy = policy or ContextPolicy()
    events = [
        reactor.new_event(
            "context_item",
            {"item": dict(it), **({"agent_context": agent_context} if agent_context else {})},
            goal=goal,
            metadata={"stream_id": f"ctx:{it.get('id', i)}"},
        )
        for i, it in enumerate(items)
    ]
    records = await asyncio.gather(*(reactor.decide_record(ev, pack, policy) for ev in events))
    plan = CompactionPlan()
    for ev, rec in zip(events, records, strict=True):
        item_id = str(ev.state["item"].get("id", ev.event_id))
        d = rec.policy_result
        plan.decisions[item_id] = d
        bucket = {"allow": plan.retain, "compact": plan.compact, "skip": plan.discard}.get(
            d.action, plan.retain
        )  # any failure or unexpected action keeps the item
        bucket.append(item_id)
    return plan

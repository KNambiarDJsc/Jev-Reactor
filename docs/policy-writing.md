# Writing policies

A policy is ordinary Python: it takes an event and Jev's typed answers and returns an
`ActionDecision`. It never executes anything, and it is unit-testable with no network.

```python
class Policy(Protocol):
    def decide(self, *, event: ReactorEvent, response: DecisionResponse) -> ActionDecision: ...
```

Optional hooks the Reactor looks for: `pre_check(event)` (hard rules, run before Jev),
`on_unavailable(event, status, config)` (what to return when Jev could not be consulted),
`risk_tier(event)`, and a `policy_id` attribute. `RuleChainPolicy` provides all of them.

## Three rules of thumb

1. **Hard facts belong in code, not in a question.** Allowlists, permissions, explicit user
   denials, amount limits, deadlines, malformed input, irreversible actions: decide them in
   `pre_check`. Jev's documentation says adversarial text in state "can move the answer", and
   an independent test of a gate found that authority-style framing moved 3 of 30 dangerous
   commands. A rule in code cannot be talked out of its answer.
2. **Never assume two answers agree.** A Noul and a Choice answer different questions; a
   Noul and its negation need not sum to 1 (measured sums ranged from 0.71 to 1.42). Compare
   them and escalate on disagreement instead of picking a favourite.
3. **When unsure, send it to a person, not to `allow`.** A policy that falls through returns
   `review`. `RuleChainPolicy` cannot default to allow.

## Bands: three outcomes, not two

A single threshold sends 0.49 and 0.51 to opposite actions although both express real
uncertainty. A `Band` has a confident-no side, a confident-yes side and an *uncertain* middle:

```python
Band(no_below=0.30, yes_above=0.70).classify(0.70)  # 'uncertain'  (both edges are strict)
```

Wire values are rounded to two decimals, so a value exactly on an edge is common; treating it
as uncertain errs toward review. The 0.30 / 0.70 defaults are the illustrative band from
TypeSafe's self-consistency cookbook, which itself says they are neither calibrated nor
optimised.

Choose edges by the cost of being wrong. Raise `yes_above` where acting on a false yes is
expensive (a refund, a page); lower `no_below` where missing a true yes is expensive (a safety
flag).

## Confidence

- **Noul has no confidence**: the number is the answer and the certainty together.
  `noul_strength(p)` (distance from a coin flip, on 0..1) stands in for it.
- **Choice and Score have a `confidence`** computed from how peaked their probabilities are.
  Low means the options overlap, the question measures more than one thing, or the state does
  not say enough.
- A decision's `confidence` is the **weakest link** across the answers its rule consulted
  (`weakest(...)`): a decision is only as certain as its least certain judgment.

Use confidence as a second axis. The answer says *what*; confidence says *whether to act*, and
riskier actions need more of it.

## A first-match rule chain

```python
# runnable
from datetime import UTC, datetime

from jev_reactor import (
    Answer,
    Band,
    DecisionResponse,
    ReactorEvent,
    RuleChainPolicy,
    decision,
    noul_strength,
)

REFUND = Band(no_below=0.30, yes_above=0.85)  # stricter than default: refunds cost money


def hard_amount_limit(event):  # plain code, before Jev, never overridable
    if event.proposed_action["amount"] > 100:
        return decision("block", "over_limit")
    return None


def refund_rule(ctx):
    p = ctx.signals.noul("refund_requested")
    verdict = REFUND.classify(p)
    if verdict == "yes":
        return decision("allow", "refund_requested", confidence=noul_strength(p))
    if verdict == "uncertain":
        return decision("review", "unclear_request", target="user")
    return decision("skip", "no_refund_requested")


policy = RuleChainPolicy(
    [("refund", refund_rule)], pre_rules=[("limit", hard_amount_limit)], policy_id="refunds/v1"
)


def event(amount):
    return ReactorEvent(
        event_id="e1",
        sequence=1,
        timestamp=datetime.now(UTC),
        event_type="refund",
        state={},
        proposed_action={"amount": amount},
    )


def answered(p):
    noul = Answer(question_id="refund_requested", type="noul", noul=p)
    return DecisionResponse(model="m", latency_ms=0, answers={"refund_requested": noul})


print(policy.decide(event=event(50), response=answered(0.95)).action)  # -> allow
print(policy.decide(event=event(50), response=answered(0.60)).action)  # -> review
print(policy.decide(event=event(500), response=answered(0.99)).action)  # -> block
```

Every decision records which rule made it (`decision.rule`), so `jev-reactor replay` and your
logs can say why. Rule names must be unique.

## Risk tiers and failure modes

When Jev cannot be consulted (timeout, error, open circuit, oversized state), the decision
comes from `ReactorConfig.failure_by_risk`, chosen by the policy's `risk_tier(event)`: `read`
falls back to your default handling, `write` goes to review, `irreversible` is blocked, and
the configuration refuses to let `irreversible` auto-allow. Give a custom policy a
`risk_tier` and it inherits this.

## Guards worth copying

- **Suppression budget.** `ToolLoopPolicy` stops skipping after `max_consecutive_skips`
  skips in a row and asks for review, so a wrong "redundant" cannot stall an agent. The
  host passes `metadata["consecutive_skips"]` (the MCP gate session tracks it for you). The
  idea is from the community's wakegate.
- **Exact duplicates in code.** Same tool, same canonical arguments, previous call succeeded,
  tool marked `idempotent`: skip without asking Jev. Jev never takes part in a match that
  code can make exactly.
- **Escalate, do not forward.** When a Noul says "call it" and the Choice confidently says
  "skip", return `review` with `signals_disagree`.

## Calibrating thresholds

Every threshold shipped here is an **initial default, not a calibrated value**. Independent
measurements of Jev disagree about whether it is over- or under-confident, and one measured
that no routing threshold transferred between two datasets. Calibrate on your own traces:

1. Run in `observe` mode ([deployment.md](deployment.md)) so nothing is blocked while you
   collect decisions.
2. Label a sample: `jev-reactor label runs.jsonl <event-id> --expected allow` (or
   `--label incorrect`). Labels go to a sidecar file; the log is never edited.
3. Read `jev-reactor report runs.jsonl`. With labels it shows action agreement and a
   calibration table by confidence bin, and it warns when fewer than about 100 labelled rows
   back the numbers (thresholds fitted on fewer should not be trusted).
4. Compare candidate thresholds without calling Jev:
   `jev-reactor replay runs.jsonl --policy strict --dry-run` lists exactly which decisions
   would change.
5. Pin the model version you tuned against; re-check when you move.

Community tools such as jevcal and Janus fit thresholds to a target accuracy on labelled data.
I have not evaluated them; the workflow above needs nothing outside this repository.

## Testing a policy

Build responses by hand (`MockProvider`'s `build_answer` or `Answer(...)` directly) and call
`policy.decide`. `tests/test_policy.py` shows the patterns: hard rules beating a confident Jev
`allow`, band edges, disagreement, the suppression budget, and Hypothesis property tests that
assert `allow` can come from only one specific combination of signals.

# Question packs

A pack is a repeatable decision task: a stable id, a version, a set of **atomic** questions,
the state paths those questions refer to, documented threshold defaults, and example scenarios.
The Reactor sends the questions; a policy turns the answers into an action.

Changing a question is a behaviour change. Every recorded event carries the pack's
*fingerprint* (a hash of every question's wording, type and criteria), so `jev-reactor report`
tells you when the questions changed in the middle of a run.

## Writing questions Jev answers well

These come from TypeSafe's documentation (Primitives, Score, Noul, Jev 1.13 jaggedness) and
from independent measurements (see [design-notes.md](design-notes.md)).

1. **One judgment per question.** "Analyze this and pick the best action" hides several
   judgments in one answer. Ask about each factor and combine the answers in code, with
   weights you control. Adding questions costs a few tokens and no extra round trip.
2. **Be literal.** Jev 1.13 answers the question you *wrote*. State the exact condition and put
   boundary cases in the criteria. If you catch yourself explaining what you really meant, that
   explanation is the missing half of the instruction.
3. **Point at the state by name.** Refer to fields with backticked dot/index paths:
   ``Does `ticket.messages[0].text` request a refund?`` The model then knows which part to judge.
4. **Noul.** Give `true` and `false` descriptions that agree with the instruction. A Noul whose
   `true` means "no" measurably performs worse. Prefer statements with no middle ground ("any",
   "states that").
5. **Choice.** Up to 255 options, each described so they separate from one another. **Always
   include an abstain option** (`other`, `unclear`, `escalate`). An independent audit measured
   accuracy on unanswerable items falling from 0.950 to 0.000 when the abstain option was
   removed, at 0.79 confidence: without one the model is forced to guess and stays confident.
6. **Score.** Two to ten levels, **numbered from 0** by position. Describe *situations*, not
   degrees ("Broken feature, but a workaround exists", not "moderate"). Each level is judged
   independently and the model never sees its number, so "worse than the previous level" means
   nothing to it. Keep one dimension per Score; split "punctual and smart and experienced".
7. **Keep arithmetic, dates and counting in code.** Jev is not a calculator; extract the parts
   (a Choice over months, say) and compute outside the model.
8. **Send only what the questions need.** Unrelated detail acts as a distractor ("context
   rot"). Do include what the assistant is *for*: one benchmark saw injection recall rise from
   74.9% to 95.1% when the request said so.
9. **Never splice untrusted text into instructions.** Put values from code, and any text you
   do not control (tool descriptions, retrieved documents, tool results), in their own state
   fields. Instructions are static and part of the fingerprint.

## The linter

`jev-reactor validate-pack <file>` (and `lint_pack` in Python) enforce what can be checked
statically. Errors fail validation; warnings fail it only with `--strict`.

| Code | Severity | What it catches | Why |
|---|---|---|---|
| `choice-missing-abstain` | error | a Choice with no `other` / `none` / `unclear` / `escalate` option (tag the question `closed-set` to waive it) | forced guessing; the 0.95 to 0.00 measurement above |
| `score-numeric-levels` | error | Score levels that are bare numbers | the model sees only descriptions; numbers give it nothing to match |
| `unresolved-state-path` | error | a backticked path not declared under `state.paths` | the state builder may not provide it, so the question refers to nothing |
| `duplicate-question` | error | two questions identical in wording and criteria | wasted tokens, double counting |
| `example-fixture-missing` | error | an `examples:` fixture that cannot be found | the pack claims a test it does not have |
| `score-degree-levels` | warning | levels like `low` / `high` | "Describe situations, not degrees" |
| `noul-criteria-inverted` | warning | a Noul `true` that reads as a negative | contradictory instructions and criteria |
| `instruction-placeholder` | warning | `{tool}`, `%s`, `{{x}}` in instructions | string-splicing; use a structured field |
| `instruction-compound` | warning | more than one `?` | hidden second judgment |
| `instruction-broad` | warning | "best course of action" style wording | ask the specific judgments |
| `instruction-long` | warning | over 600 characters | keep questions short |

## The state contract

A pack declares which paths its state provides, and which list fields may be trimmed
(oldest first) to fit the size limit:

```yaml
state:
  paths: [goal, agent_context, proposed_call.tool, proposed_call.arguments, recent_calls[].tool]
  trim_lists: [recent_calls]
```

The pack's Python `state_builder` produces exactly those paths; a test in this repository checks
that the built-in builders never emit an undeclared path. A path with a wildcard-free index
(`recent_calls[0].tool`) matches a declared `recent_calls[].tool`.

## The file format

Built-in packs are Python (the source of truth) and are exported as YAML with
`jev-reactor export-pack tool-loop`; a test fails if the committed YAML drifts. To make your
own, export one, change its `id`, edit the questions, and validate it:

```yaml
id: support-triage
version: "1"
state:
  paths: [ticket.text, ticket.customer_tier]
questions:
  wants_refund:
    type: noul
    instructions: Does `ticket.text` ask for money back?
    criteria:
      true: The ticket asks for a refund or a chargeback.
      false: The ticket asks for something else.
  topic:
    type: choice
    instructions: What is `ticket.text` mainly about?
    criteria:
      billing: Charges, invoices, refunds.
      shipping: Delivery, tracking, damage in transit.
      other: Anything else, or unclear.
```

A YAML pack has no state builder, so the Reactor sends the event's `state` (plus `goal` and
`proposed_action`) as given: build that state to match `state.paths`. Write a Python
`state_builder` when you want redaction-friendly shaping or trimming beyond that.

```python
# runnable
from jev_reactor import QuestionPack, lint_pack

pack = QuestionPack.from_dict(
    {
        "id": "support-triage",
        "state": {"paths": ["ticket.text"]},
        "questions": {
            "wants_refund": {
                "type": "noul",
                "instructions": "Does `ticket.text` ask for money back?",
                "criteria": {
                    "true": "The ticket asks for a refund.",
                    "false": "It asks for something else.",
                },
            },
            "topic": {
                "type": "choice",
                "instructions": "What is `ticket.text` mainly about?",
                "criteria": {
                    "billing": "Charges and invoices.",
                    "shipping": "Delivery.",
                    "other": "Anything else, or unclear.",
                },
            },
        },
    }
)
print(len(lint_pack(pack)), "issues")  # -> 0 issues

forced_guess = QuestionPack.from_dict(
    {
        "id": "x",
        "questions": {
            "topic": {
                "type": "choice",
                "instructions": "Which?",
                "criteria": {"a": None, "b": None},
            }
        },
    }
)
print([i.code for i in lint_pack(forced_guess)])  # -> ['choice-missing-abstain']
```

## Testing a pack

- **Scenarios.** A fixture is a JSON file with an `event`, recorded `answers`, and the
  `expected` action. `MockProvider.from_fixture(path)` replays the answers, so the
  policy and the pack can be tested with no network. Fixtures are hand-written; they say so
  (model `mock-fixture`).
- **Examples.** List fixtures under `examples:` with `expect_action`; `validate-pack` checks the
  files exist (in `fixtures/` or `tests/fixtures/`).
- **Real data.** Fixtures show the shape, not the accuracy. To learn how a pack behaves on your
  traffic, run it in `observe` mode, label decisions, and read `jev-reactor report`
  ([policy-writing.md](policy-writing.md)).

## Built-in packs

| Pack | Questions | Policy |
|---|---|---|
| `tool-loop` | `relevant`, `should_call`, `redundant`, `task_complete`, `needs_user_input`, `injection_suspected` (Noul); `next_action` (Choice) | `ToolLoopPolicy`: hard rules, then a first-match chain that escalates on disagreement |
| `context-retention` | seven Nouls plus `preservation_need` (Score) about *one* item | `ContextPolicy`: pins in code, then keep unless several signals agree |
| `agent-loop` | `goal_satisfied`, `needs_user_input` (Noul); `model_tier` (Choice) | `AgentLoopPolicy`: stop, ask, or route (one tier up on low confidence) |

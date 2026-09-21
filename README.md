# Jev Reactor

An open-source runtime for real-time typed decisions. It uses Jev for parallel semantic
judgments and ordinary code for policy, control flow, and side effects.

```bash
pip install "git+https://github.com/KNambiarDJsc/Jev-Reactor"   # not on PyPI yet
jev-reactor init                                # copies examples, fixtures and packs here
jev-reactor run examples/tool_loop.py --mock    # offline: recorded answers, no key, no network

export TYPESAFE_API_KEY=...                     # then, against live Jev:
jev-reactor run examples/tool_loop.py
```

```text
Decision: skip
Reason: redundant_tool_call
Redundancy probability: 0.91
Choice confidence: 0.86
```

That is the output of `examples/basic_decision.py --mock`. In mock mode the answers are
hand-written fixtures, **not** Jev output; run it live to see what Jev says. Jev returns typed
values but can still make semantic mistakes. Nothing here is a guarantee that a decision is
correct.

## What it is

A software event arrives (an agent proposes a tool call, a context item ages, a turn ends).
Jev Reactor turns it into a compact, redacted state, asks Jev several small typed questions
in **one** request, and hands the answers to a deterministic Python policy. The policy returns
an immutable action. **The Reactor never executes anything;** your application does, and only
for the actions you choose.

```text
event -> bounded state -> parallel Jev questions -> typed answers -> policy -> action -> host
         (redacted)       (one request)             (probabilities)   (plain Python)
```

Jev judges, code decides, adapters execute. It is not a chatbot, an agent framework, or a
safety guarantee, and it does not replace Jev.

## Use it from Python

```python
# runnable
import asyncio

from jev_reactor import MockProvider, Reactor, ToolLoop, ToolSpec

tools = [
    ToolSpec(
        name="search_invoices", risk="read", idempotent=True, requires_permission="invoices:read"
    ),
    ToolSpec(name="send_email", risk="irreversible", requires_permission="email:send"),
]
loop = ToolLoop(tools)  # a question pack (what Jev is asked) + a policy (what happens)

# Recorded answers stand in for Jev. Swap in TypeSafeProvider() (needs TYPESAFE_API_KEY).
provider = MockProvider(
    {
        "relevant": 0.96,
        "should_call": 0.94,
        "redundant": 0.04,
        "task_complete": 0.03,
        "needs_user_input": 0.02,
        "injection_suspected": 0.01,
        "next_action": ("call_tool", 0.91),
    }
)


async def main() -> None:
    async with Reactor(provider, pack=loop.pack, policy=loop.policy) as reactor:
        event = reactor.new_event(
            "tool_call_proposed",
            {"recent_calls": []},
            goal="Find the status of invoice INV-2041",
            proposed_action={"tool": "search_invoices", "arguments": {"invoice_id": "INV-2041"}},
            metadata={"permissions": ["invoices:read"]},
        )
        decision = await reactor.decide(event)
        print(decision.action, decision.reason_codes)  # -> allow ['relevant_and_useful']

        # A hard rule decides this before Jev is asked; no answer can override it.
        mail = reactor.new_event(
            "tool_call_proposed",
            {},
            goal="Tell the customer",
            proposed_action={"tool": "send_email", "arguments": {"to": "a@b.c"}},
            metadata={"permissions": ["email:send"]},
        )
        held = await reactor.decide(mail)
        print(held.action, held.reason_codes, held.provider_status)
        # -> review ['irreversible_needs_approval'] not_called


asyncio.run(main())
```

## The three question types

| Type | Asks | Returns | Notes |
|---|---|---|---|
| **Noul** | Is this true? | `noul` in 0..1 | No separate confidence: the number is the answer and the certainty. |
| **Choice** | Which of these options? | `choice`, `probabilities`, `confidence` | Up to 255 options. **Always include an abstain option** (`other`, `unclear`). |
| **Score** | Which level? | `score`, `probabilities`, `confidence` | 2 to 10 ordered levels, **numbered from 0**. Describe situations, not degrees. |

Ask many narrow questions about one state in one request; they run in parallel. Combine the
answers in code. See [docs/question-packs.md](docs/question-packs.md).

## What is different about how this uses Jev

Jev's own documentation and the community's measurements shaped several choices that differ
from the obvious approach. The full list, with sources, is in
[docs/design-notes.md](docs/design-notes.md). The short version:

- **Your hard rules run first and never depend on Jev**: allowlist, permissions, user denials,
  amount limits, approval for irreversible actions, exact-duplicate calls. Jev's docs say
  adversarial text in state "can move the answer".
- **Two-sided thresholds.** Between "confident no" and "confident yes" is *uncertain*, and
  uncertain goes to review instead of being forced to a coin flip.
- **Answers are not assumed to agree.** A Noul and a Choice answer different questions, so
  disagreement is escalated, not resolved silently.
- **Fail safe, by risk.** When Jev is unavailable, reads fall back to your default handling,
  writes go to review, and irreversible actions are blocked. Nothing defaults to allow.
- **When in doubt, keep it** (context retention): pins are decided in code, discarding needs
  several signals to agree, and Jev never rewrites text.
- **Replay without Jev.** Every decision is recorded (a digest of the redacted state, not the
  state); `jev-reactor replay` recomputes policy over the recorded answers and shows what a
  new threshold set would change.

## Command line

```text
jev-reactor init                       scaffold examples, fixtures and packs here
jev-reactor run <file.py> [--mock]     run an example
jev-reactor replay <runs.jsonl> [--policy strict] [--dry-run]
jev-reactor report <runs.jsonl> [--json]
jev-reactor validate-pack <pack.yaml>  lint a question pack
jev-reactor inspect <file>             pretty-print a fixture, event, pack or run
jev-reactor label <runs.jsonl> <event-id> --expected allow
jev-reactor export-pack tool-loop      a built-in pack as YAML, to start your own
jev-reactor bench [--live]             measure latency on your machine
```

There is no telemetry and nothing is uploaded. A test checks that no core module can open a
network connection.

## Security and data: read this before using it on real data

- Redaction (secret-shaped keys and credential patterns) runs before every provider call and
  every write. It is **best effort**: keep secrets out of state, and allowlist the fields you
  send.
- By default a record stores a **digest** of the redacted state. No goal text and no raw
  provider payload are written unless you opt in.
- **State you send is processed by TypeSafe.** Read their Data Processing Agreement and Privacy
  Policy first; zero data retention is offered for enterprise customers.
- **Recorded logs contain TypeSafe Output. Do not use them to train or distill a model:**
  TypeSafe's Master Customer Agreement (section 2.3(b)) prohibits it.
- This is a decision gate, **not a sandbox**. Stack OS-level isolation underneath. See
  [SECURITY.md](SECURITY.md).

## Measuring performance

This README makes no latency, accuracy, or cost claims for Jev, and the sample data in
`examples/replay.jsonl` is hand-written (model `mock-fixture`), not measured. To measure your
own setup:

```bash
jev-reactor bench            # policy and Reactor overhead on this machine, no network
jev-reactor bench --live     # a small number of real requests through your network and account
```

The engineering target for local policy evaluation is p95 under 5 ms; `bench` prints whether
your machine meets it. Nothing about real-time behavior against Jev is asserted until you run
`--live`. Thresholds in every pack are **initial defaults, not calibrated values**; calibrate
them on your own labelled traces ([docs/policy-writing.md](docs/policy-writing.md)).

## Documentation

- [Architecture](docs/architecture.md): lifecycle, failure semantics, concurrency, persistence
- [Providers](docs/providers.md): the TypeSafe adapter, the mock, writing your own
- [Question packs](docs/question-packs.md): writing questions Jev answers well, and the linter
- [Policy writing](docs/policy-writing.md): bands, confidence, rule chains, calibration
- [Deployment](docs/deployment.md): modes, deadlines, rate limits, privacy
- [Design notes](docs/design-notes.md): what the docs and community changed, with sources

## Status and roadmap

Version 0.1.0 is a local runtime and developer CLI; the API may change before 1.0.

| Next | Not planned |
|---|---|
| Real MCP protocol adapter, TypeScript runtime, LangGraph middleware, OpenTelemetry spans, provider fallback chains, speculative prefetch | Anything that trains or distills a model on Jev output (prohibited by TypeSafe's terms), a hosted dashboard in this repository |

## License

Apache-2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE) and
[docs/dependency-notices.md](docs/dependency-notices.md). Jev and TypeSafe are TypeSafe AI's
products; this project is independent and not affiliated with or endorsed by TypeSafe AI.

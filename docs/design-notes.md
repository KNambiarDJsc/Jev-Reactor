# Design notes: what the docs and the community changed

This project began as a written brief. Before building it I read TypeSafe's documentation end
to end (the `llms-full.txt` export of docs.typesafe.ai, 2026-09-21), the official Python SDK
reference, the Jev 1.13 "jaggedness" page, and what the community has built and measured
around Jev. Several parts of the brief did not survive contact with that evidence. This page
records each change, why, and where the evidence lives, so any of it can be checked or
challenged.

Community results are quoted **as their authors report them**. I did not reproduce them, and
none of them are performance claims about this project.

## Changes to the brief

| # | The brief said | This project does | Evidence |
|---|---|---|---|
| 1 | Test with `respx`; `pydantic>=2.7` | `httpx2.MockTransport` drives the *real* SDK; `pydantic>=2.12` | `typesafe-sdk` 0.7.0 depends on `httpx2` (not `httpx`) and `pydantic>=2.12`. I confirmed `respx` cannot intercept it and that `MockTransport` can. |
| 2 | Answer `probabilities` / `legend` keyed by `str` | Normalised to `str`; the SDK hands back `int` for Score | SDK `ScoreAnswer` docs: "keys `probabilities` and `legend` by integer level". Wire format uses strings. |
| 3 | Noul answers have `confidence` | Noul has **none**; Choice and Score do | Docs, Noul page: "There is no separate `confidence` value for a Noul." |
| 4 | Probability sum tolerance `1e-3` | Rounding-aware (`0.005 x n`, capped at `0.05`) | Wire values are two-decimal (docs examples; jev-orderby-bench measured ties at two decimals). Three options at 0.33 sum to 0.99 and a flat `1e-3` would reject a legitimate response. |
| 5 | Score range "declared" | Levels are **0-indexed**: `n` levels give `[0, n-1]` | Docs, Score page: "A level's number is its position in the `criteria` array, starting at 0." |
| 6 | One `next_action` Choice decides the next step | It stays, but as one *routing* Choice among seven atomic questions; the policy cross-checks it against the Nouls | Docs: ask "one snap judgment per question"; "Analyze this message and determine the best course of action" is the named anti-example. Jaggedness #8: a Noul and a Choice "are not guaranteed" to agree. |
| 7 | `context_value` Score with levels Discard / Compact / Retain | Moved to the context pack, one dimension ("how much exact content does the goal still need"), levels describe *situations* | Docs, Score page: "Describe situations, not degrees"; "Keep each Score question to one dimension"; levels are judged independently and the model never sees their numbers. |
| 8 | Single threshold per Noul | Two-sided `Band`: confident no, **uncertain**, confident yes; uncertain goes to review | Docs, Noul page: "Values in the middle can go to a person." Self-consistency cookbook uses 0.30 / 0.70 and calls it "illustrative ... neither a calibrated guarantee nor an optimized threshold." |
| 9 | Confidence gate on the Choice only | Per-action risk tiers; decision confidence is the **weakest link** across the answers a rule used | Docs, confidence-routing pattern (0.6 floor, higher for risky actions). Function-calling cookbook: a request's confidence "is the least certain judgement behind" it. |
| 10 | Choice options as in the brief | **Every Choice needs an abstain option**; enforced by `validate-pack` | Independent audit (jev-calibration-audit): removing the abstain option took accuracy on unanswerable items from 0.950 to 0.000, at 0.79 confidence. Docs also advise adding `other`. |
| 11 | Timeout via `asyncio` only | The adapter passes the SDK an explicit `RetryPolicy(timeout=deadline)`; the Reactor also enforces the deadline itself | I measured the SDK default: a failing call took 1.22 s over 3 attempts, and the default retry budget is 30 s. That is longer than any interactive deadline. |
| 12 | Redact state | Redact **and** pin the SDK's own logger to INFO unless you opt in | The SDK logs full request/response bodies (your state) at DEBUG; docs: "request and response bodies are not [redacted]". |
| 13 | Persist events with state | Default persists a **digest** of the redacted state, no goal text, no raw response | Brief: "Raw state is not persisted by default." A test caught the goal text leaking into the default record. |
| 14 | `response.request_id` is available | Treated as optional | The SDK property *raises* when the `x-typesafe-request-id` header is absent (for example behind a gateway). A contract test caught this. |
| 15 | Fixture-driven mock | Fixtures declare themselves hand-written: model `mock-fixture`, no latency | The brief forbids fabricated performance numbers; a `jev-1.13.0` label and invented latencies in sample data would read as real measurements. |
| 16 | "Jev Distill pipeline for local small models" on the roadmap | **Not built.** A local provider is an interface only | TypeSafe Master Customer Agreement, section 2.3(b), prohibits using the Services or any Output "to perform model distillation, train a model to imitate the output of the Services, or develop ... a similar or competing product". Recorded logs contain Output. See below. |

## Additions the community evidence supports

| Addition | Where it comes from |
|---|---|
| **Hard rules run before Jev and before state building**; a denial is never overridden by any Jev answer | jev-gate (eugeniughelbur/jev-engineering): "Denials run before the allowlist on purpose ... `cat` is harmless until it is `cat ~/.ssh/id_ed25519`", and a test pins the order "because this shipped the wrong way round once". pi-verdict: "Deterministic floor before AI". |
| **Modes: `observe` / `guard` / `enforce`**, and **hard rules bind in every mode** | jev-gate ships `observe` as the default and says to read its log "for a week before you turn anything on". Reactor goes one step further: a mode only relaxes Jev's verdicts, never your own constraints. |
| **Exact duplicate detection in code**; Jev is never asked | pi-jev-context: "Read deduplication remains deterministic and Jev never participates in its matching decision." Docs: "Use code when you can." |
| **Suppression budget**: after N skips in a row, review instead | wakegate: skips a wakeup only below 0.2 on "wake", and forces a wake after `maxSkips` in a row. |
| **"When in doubt, keep it"** for context, with retain-only pins decided in code | pi-jev-context ("When in doubt, keep it"; guards retain failures, warnings, stack traces); jev-gate `keep` ("Nothing is rewritten or summarised"). |
| **Out-of-range probabilities are rejected, never clamped** | pi-jev-context: "Out-of-range Jev probabilities are rejected, never clamped into a hide decision." |
| **Escalate on Noul/Choice disagreement**; never assume structural invariants | Jaggedness #8. jev-calibration-audit measured complements summing from 0.71 to 1.42 and a Noul vs a two-option Choice differing by 0.125 on average. |
| **Deployment context in state** (`agent_context`) | jev-sec-bench: on the same 662 prompt-injection messages, telling Jev what the assistant is for raised recall from 74.9% to 95.1%. |
| **One item per request** for the context pack | jev-orderby-bench: rows sent through a 40-row batched state failed a ranking gate that one-row-per-request passed. (Batching *questions* about one state is fine; see next row.) |
| **All questions in one request** | Docs' parallel-questions cookbook; jev-calibration-audit: 16 bundled questions vs 1 shifted confidence by 0.008 and flipped 0.4% of answers. |
| **Step up one model tier, not to the top**, on low routing confidence | jev-gate `route`. |
| **Record the resolved model id; warn on mixed versions** | Docs, Models page: `jev-latest` "moves when a new release ships"; "If you have tuned confidence thresholds against a specific version, pin that version's ID". jevcal makes the same point. |
| **Labels sidecar, and no accuracy claim without labels; a small-sample warning** | jevcal: thresholds fitted on fewer than about 100 labelled rows "should not be trusted". Janus: "no routing parameter transferred between the two datasets" and it "ships no default threshold". |
| **Replay never calls a provider** | The docs' own cookbooks cache every API call and replay it; identical requests are *not* deterministic (jev-calibration-audit: 50 identical requests gave 15 distinct answers), so recorded responses are the only stable input. |
| **Untrusted text lives in fixed state fields, never in instructions** | Docs: put values from code "in its own field instead of splicing them into a string template". RAG-passage cookbook detects instructions aimed at the model with a Noul. |
| **Fail-closed by risk tier; an unreachable provider never auto-allows an irreversible action** | jev-use: "an unreachable backend escalates instead of allowing"; pi-verdict: "uncertainty produces friction, never permission". |
| **The provider interface stays neutral** | warmersun's claims-vs-evidence review and primeline's pre-registered comparison (where a smaller general model matched Jev on two jobs) argue against hard-wiring one vendor. The official System One Adapter mirrors `system_one` over OpenAI/Anthropic. |
| **Not a sandbox** | pi-verdict, Bicameral, jev-gate all say so. So does this project. |

## What I could not verify

- **Whether TypeSafe restricts publishing Jev benchmark numbers.** The community tool jevcal
  states that "TypeSafe's customer agreement restricts publishing performance numbers for
  Jev". I read the public Master Customer Agreement (typesafe.ai/legal/mca) and found the
  license restrictions in section 2.3 but no such clause. It may live in an order form. To be
  safe regardless: **this repository publishes no Jev performance or accuracy numbers.**
  `jev-reactor bench --live` lets you measure your own.
- **Any calibration claim.** The independent measurements disagree with each other (under- and
  over-confident on different corpora). That is exactly why every threshold here is documented
  as a starting point, and why `report` shows calibration only for events *you* have labelled.
- **Live behavior against the real API.** The default test suite makes no network calls and
  needs no key. The provider is exercised through the real SDK against an in-process
  transport, and there is one gated live smoke test (`TYPESAFE_LIVE_TESTS=1`).

## Compliance notes

- **Do not train on recorded runs.** Decision logs contain TypeSafe Output. Section 2.3(b)
  of the Master Customer Agreement prohibits using Output to train or distill a model or to
  build a competing product. Reactor therefore has no export-for-training feature, and a local
  provider (see `docs/providers.md`) must be trained on data *you* labelled, not on Jev's
  answers. This is a plain reading of a contract, not legal advice; read it yourself.
- **Security testing.** Section 2.3(g) also prohibits security or vulnerability testing of the
  Services. The injection and abuse tests in this repository run against the mock provider to
  test *this* project's policy code; the gated live test sends one benign request.
- **Data.** State you send is processed by TypeSafe. See their Data Processing Agreement and
  Privacy Policy; zero data retention is offered for enterprise customers.

## Sources

Official documentation (docs.typesafe.ai): introduction, System One, State, Primitives, Score,
Noul, Choice, Advanced structure, Confidence, How to build with System One, Patterns
(confidence-gated routing, fan-out, composite scoring, intent routing), Models, Jev 1.13
jaggedness, HTTP API, Python SDK reference (clients, questions, responses, retries,
exceptions, constants), and the cookbooks on self-consistency, parallel questions, RAG-passage
classification, function calling, and LLM guardrails.

Community projects and measurements (read 2026-09-21; see the awesome-typesafe list):

- github.com/eugeniughelbur/jev-engineering (jev-gate)
- github.com/Nyarlathoteppppp/pi-jev-context
- github.com/shitianfang/wakegate and github.com/shitianfang/jev-use
- github.com/jesset/pi-verdict and github.com/AbdelStark/bicameral
- github.com/jujumilk3/jev-calibration-audit
- github.com/Gaurav-Gosain/jev-sec-bench
- github.com/yodablocks/jev-orderby-bench
- github.com/FirasSX914/Janus and github.com/abhixhek/jevcal
- github.com/typesafe-ai/system-one-adapter-python
- warmersun.com/jev (claims versus evidence)

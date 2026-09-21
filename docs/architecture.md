# Architecture

```text
+-----------------------------------------------------------------------+
| Host application: agent / MCP client / browser / simulator / game     |
+-----------------------------+-----------------------------------------+
                              | event
                              v
+-----------------------------------------------------------------------+
| Reactor                                                               |
|  1. hard rules   (policy.pre_check: event only, no Jev, no state)     |
|  2. state        (pack builder -> allowlist -> redact -> size bound)  |
|  3. breaker      (open circuit fails fast, no provider call)          |
|  4. provider     (ONE request, all questions, under a deadline)       |
|  5. validate     (answers match the questions; never clamp)           |
|  6. policy       (plain Python over typed answers -> ActionDecision)  |
|  7. record       (DecisionEvent -> sinks: JSONL / stdout / your own)  |
+-----------------------------+-----------------------------------------+
                              | immutable ActionDecision
                              v
+-----------------------------------------------------------------------+
| Host application decides whether and how to act. The Reactor never    |
| executes anything.                                                    |
+-----------------------------------------------------------------------+
```

## The lifecycle, in the order the code runs it

`Reactor.decide_record` in `src/jev_reactor/reactor.py`:

1. **Hard rules first.** `policy.pre_check(event)` sees only the event. If it returns a
   decision, that is the answer: Jev is not called and the state builder does not run
   (`provider_status = "not_called"`). Hard rules therefore cannot be swayed by state content
   and still work if the state builder is broken.
2. **State.** The pack's builder produces a compact dict; an optional field allowlist trims it;
   the redactor masks secrets and caps sizes; `bound_state` drops the *oldest* items of named
   list fields to fit `max_state_chars`. If it still does not fit, the decision is a fail-safe
   `state_too_large` and nothing is sent.
3. **Circuit breaker.** After repeated transient failures the circuit opens and decisions fail
   fast (`circuit_open`) instead of spending a deadline each. One probe is allowed after the
   recovery time.
4. **One request.** Every question goes to the provider together. The Reactor holds a semaphore
   slot, hands the provider the *remaining* deadline, and cancels the call at the deadline.
5. **Validation.** `validate_response` checks that every question was answered, types match,
   Choices are declared options, Score values lie in `[0, n-1]`. A bad answer is an error
   decision, never repaired.
6. **Policy.** `policy.decide(event, response)` returns an `ActionDecision`. A policy that
   raises, or returns something that is not a valid decision (for example an unknown action),
   degrades to `review` with reason `policy_error` (or raises, with
   `strict_policy_errors=True`, which the test suite uses).
7. **Record.** A `DecisionEvent` goes to every sink. A failing sink is logged (never with the
   record) and never breaks a decision.

## Failure semantics

Every decision has an explicit outcome. Nothing silently defaults to allow.

| `provider_status` | Meaning | Decision |
|---|---|---|
| `ok` | Jev answered and the answers validated | the policy's decision |
| `not_called` | a hard rule, or an oversized/broken state, decided first | that rule's decision, or the fail-safe mode |
| `timeout` | no answer within the deadline | fail-safe mode for the risk tier |
| `error` | provider error, or a malformed answer | fail-safe mode; reason includes the error kind |
| `circuit_open` | the circuit is open | fail-safe mode |
| `stale` | a newer event on the same stream superseded this one | `fallback` / `superseded` |
| `late` | (a *second* record) an answer arrived after the deadline | recorded, never applied |

Fail-safe modes come from `ReactorConfig.failure_by_risk`; the defaults are:

| Risk tier | On failure | Why |
|---|---|---|
| `read` | `fallback` | the host applies its own default handling |
| `write` | `review` | a person looks |
| `irreversible` | `block` | and the configuration **rejects** `allow` for this tier |
| unknown tool | `block` | most conservative |

Errors are classified so the breaker does the right thing: timeouts, 5xx and 429 count toward
opening it; a 401/403 opens it immediately (retrying bad credentials is noise); a 400/422 or a
malformed answer is a pack bug, not an outage, and does not count. A 429's `Retry-After`
lengthens the open period. Configuration errors, such as a missing API key, are **not** turned
into outage decisions: they raise.

## Streaming: `Reactor.run`

```text
events --> feeder --> [ bounded queue, max_in_flight ] --> consumer --> yields decisions
             |                                                  ^
             +--> one task per event --> semaphore --> provider |
```

- **Order.** Decisions are yielded in the order events arrived.
- **Backpressure.** The queue holds at most `max_in_flight` decisions, so a slow consumer
  slows the source instead of growing memory. Tasks are never created without bound.
- **Concurrency.** At most `max_in_flight` provider calls run at once, including any kept alive
  past a deadline to record a late result.
- **Stale events.** With `stale="cancel"`, a newer event on the same stream (`metadata`
  `stream_id`, else `session_id`, else one shared stream) marks older in-flight decisions
  superseded and cancels their calls. Whatever such a call returns is discarded: a response
  can never drive a decision about a newer situation. Use one stream per session, or
  `stale="keep"`.
- **Late results.** With `late_grace_seconds > 0` a call is kept alive briefly after its
  deadline; if it finishes, a second record (`late`) is written with what the policy *would*
  have decided, to help you tune the deadline. It is never applied to the decision already
  returned.
- **Cancellation.** Closing the stream (or cancelling the consumer) cancels the feeder, every
  in-flight decision, and closes the source iterator. A test asserts no task is left running.

## What is persisted

`DecisionEvent` (one JSONL line, append-only, flushed per record):

| Field | Default | Notes |
|---|---|---|
| `event` (id, sequence, timestamp, type, `proposed_action`, `metadata`) | redacted | needed to re-derive hard rules on replay |
| `event.state` | **empty** | only with `persist_state="redacted"` |
| `event.goal` | **omitted** | only with `persist_state="redacted"` |
| `state_digest` | sha256 of the redacted state | lets you see two events had the same state |
| `response.answers` | kept | probabilities, choices, scores, confidence |
| `response.raw` | **dropped** | only with `persist_raw_response=True` |
| `response.model` | the *resolved* id, e.g. `jev-1.13.0` | not the alias you asked for |
| `question_pack_id/version/fingerprint`, `policy_id` | kept | a changed question is a visible behaviour change |
| `policy_result` | kept | action, reasons, confidence, the rule that fired, signals |
| `outcome` | empty | labels live in a sidecar file; the log is never edited |

Because a policy may read `event.state` (for example exact-duplicate detection), replaying a
`not_called` decision that depended on state needs `persist_state="redacted"` at record time;
otherwise replay keeps the recorded decision and says so.

## Module map

| Module | Job |
|---|---|
| `models.py` | the JSON-serialisable data contract |
| `reactor.py` | lifecycle, deadlines, breaker use, streaming |
| `policy.py` | bands, weakest-link confidence, first-match rule chains |
| `packs/` | `tool_loop`, `context`, `agent_loop`: questions, state builders, policies |
| `questions.py` | pack format, linter, response validation |
| `providers/` | the interface, the TypeSafe adapter (the only SDK importer), the mock |
| `redaction.py`, `state.py` | what leaves the process |
| `breaker.py` | circuit breaker |
| `sinks/` | JSONL, stdout, memory |
| `replay.py`, `metrics.py` | recompute policy over a recording; reports |
| `adapters/mcp.py` | tool gate over MCP-shaped tools, plus a labelled demo server |
| `cli.py`, `demo.py` | the command line and shared example scaffolding |

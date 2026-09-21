# Deployment

Version 0.1 is a local runtime and developer CLI. There is no service to deploy: you embed the
`Reactor` in your host application. This page is the checklist for doing that responsibly.

## Roll out in three stages

Start where nothing can go wrong and tighten as you gain evidence. The MCP-shaped gate
(`jev_reactor.adapters.ToolGate`) has three modes:

| Mode | What binds | Use it |
|---|---|---|
| `observe` | only **your hard rules** (allowlist, permissions, denials, limits, approval for irreversible actions); Jev's verdicts are recorded as `would_execute_if_enforced` | first, for a week or more, while you collect and label decisions |
| `guard` | your hard rules, plus Jev's safety verdicts (`block`, `review`) | once the hard rules and the injection question look right on your traffic |
| `enforce` (default) | everything except `allow` is held back | once thresholds came from your own labelled log |

Hard rules bind in every mode. A mode only relaxes what *Jev* decided, never what you declared.
If you embed the Reactor directly, the same staging applies to how your host reacts to a
decision.

## Deadlines, retries, concurrency

- `ReactorConfig.deadline_seconds` (default 1.0) is both the provider's timeout and the
  Reactor's own cap. Choose it from *your* measured latency (`jev-reactor bench --live`), not
  from anyone's claim.
- The SDK's retries are bounded by that deadline; the default here is zero retries.
- `max_in_flight` (default 8) bounds concurrent provider calls and queued decisions in
  `run()`. TypeSafe's Models page states a request-rate limit and says the limits change
  dynamically, so leave headroom. A 429 is translated, its `Retry-After` lengthens the
  circuit-breaker's open period, and it counts toward opening it.
- The **circuit breaker** (5 consecutive transient failures, 10 s recovery by default) makes an
  unhealthy provider fail fast instead of costing every decision a full deadline.

## When Jev is unavailable

Decide this per risk tier before you need it (`ReactorConfig.failure_by_risk`). Defaults: reads
fall back to your own handling, writes go to review, irreversible actions are blocked, and
`irreversible` cannot be configured to allow. See [architecture.md](architecture.md).

## Privacy and data handling

- **State you send is processed by TypeSafe.** Read their Data Processing Agreement and
  Privacy Policy (linked from their Legal docs page). Zero data retention is offered to
  enterprise customers.
- **What is written locally** (see the table in [architecture.md](architecture.md)): by default
  a digest of the redacted state, the answers, and the decision. Raw provider payloads and goal
  text are opt-in. Nothing is uploaded anywhere; there is no telemetry.
- **Redaction is best effort.** It masks values under secret-shaped keys and well-known
  credential formats, scrubs the configured API key wherever it appears, and caps string, list
  and depth sizes. It cannot know what your data treats as sensitive. Prefer
  `ReactorConfig.state_allowlist` so only named fields ever leave the process, and never put
  secrets in `metadata`.
- **Logs.** Decision logs and labels are private by default (`decisions/` is git-ignored in
  scaffolds). They contain TypeSafe Output: **do not use them to train or distill a model**
  (Master Customer Agreement, section 2.3(b)).
- **Secrets.** The API key comes from the environment or a local `.env` file (never committed).
  It is never logged, and error messages never include request bodies.

## Cost accounting

Each decision's `response.usage` records input tokens, and `jev-reactor report` totals them.
TypeSafe's Models page states a price per million input tokens with output not billed (at the
time of writing, 2026-09-21); multiply by the total yourself. State size dominates: the same
state is sent once per request however many questions you ask, which is why all questions
share one request.

## Model versions

`jev-latest` moves when a new release ships. Pin a versioned id (`TYPESAFE_DEFAULT_MODEL`)
once you have thresholds, and upgrade deliberately: record a run on the new version, then
compare with `jev-reactor report` (it warns about mixed versions) and `replay`.

## Running in CI

- Tests and tutorials need no credentials: use `--mock` and `MockProvider`.
- The live smoke test is opt-in (`TYPESAFE_LIVE_TESTS=1`); never make it the default.
- The included workflow runs lint, strict type checking, the tests on Python 3.11 to 3.13, a
  dependency vulnerability scan (`pip-audit`), and a build that installs the wheel and runs the
  README flow.

## What this is not

A decision gate is not a sandbox. It reduces how often an agent does something pointless or
unapproved; it does not contain an agent that is already compromised. Stack OS-level isolation
(containers, least-privilege credentials, network egress rules) underneath, and treat
[SECURITY.md](../SECURITY.md) as the description of what is and is not claimed.

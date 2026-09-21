# Security policy

Jev Reactor processes potentially sensitive agent state and sits in front of actions that may
matter, so security is a design constraint and not an afterthought. It is also **not a
security guarantee**. Read "What this does not claim" before relying on it.

## Reporting a vulnerability

Please report vulnerabilities **privately**, not in a public issue.

1. Use GitHub's private vulnerability reporting: on the repository page, open the **Security**
   tab and choose **Report a vulnerability** (available once the maintainers have enabled it).
2. If that option is not available, open a public issue titled "Security contact request"
   **without any details**, and a maintainer will arrange a private channel.

Please include the version, what you did, what you expected, and what happened. Reports are
handled on a best-effort basis; there is no SLA. We will credit reporters who want credit.

**In scope:** this repository's code, its defaults, and its documentation where it could lead a
user to an unsafe configuration (for example a default that allows an action it should hold, or
a path that writes a secret or raw state to disk).

**Out of scope:** vulnerabilities in TypeSafe's service or in Jev itself (report those to
TypeSafe), and vulnerabilities in third-party dependencies that are already public (they are
scanned in CI; open an issue to bump the pin).

Please do not use this project's tests or examples to probe TypeSafe's hosted service. Their
Master Customer Agreement (section 2.3(g)) prohibits security or vulnerability testing of the
Services. Everything in this repository that exercises attacks (injection, malicious arguments,
stale responses) runs against the offline mock provider to test *this project's* policy code.

## Supported versions

Only the latest release receives fixes while the project is pre-1.0.

## What this does not claim

- **It is not a sandbox.** It decides whether to allow a proposed action; it does not contain a
  process that is already compromised. Stack OS-level isolation, least-privilege credentials
  and network egress rules underneath.
- **Jev can be wrong and can be swayed.** Its answers are typed and constrained to your options,
  but the chosen option can still be incorrect. TypeSafe's own documentation says text in
  state "can move the answer" and that calibration "does not guarantee that an individual
  answer is correct". Nothing safety-critical here depends on a Jev answer alone: hard rules
  decide first, and a Jev verdict can only make a decision *more* cautious than the hard rules
  allow.
- **Redaction is best effort.** It cannot know what your data considers secret.
- **No accuracy or latency claims** are made for Jev. Thresholds are initial defaults, not
  calibrated values.

## Threat model and where each mitigation is tested

| Threat | Mitigation | Test |
|---|---|---|
| Prompt injection in a tool description | descriptions reach Jev only as data under `proposed_call.description`, never in instructions; hard rules do not read them; an `injection_suspected` Noul routes to review | `test_untrusted_text_never_reaches_the_instructions`, `test_injection_flag_never_lets_the_call_through` |
| Prompt injection in a retrieved document or tool result | results are capped excerpts in fixed state fields; same Noul; a flagged call is never allowed on Jev's other answers | `injection_tool_call` fixture in `test_policy.py`, `test_a_held_back_action_is_never_executed[injection]` |
| Malicious arguments | amounts checked in code (booleans, NaN, negatives, strings rejected); duplicate detection uses canonical JSON; the allowlist decides the tool | `test_amount_limits_are_enforced_in_code`, `test_duplicate_detection_is_conservative` |
| Tool not in the allowlist | blocked before Jev; an unlisted tool can never be allowed (property test) | `test_hard_rules_beat_a_confident_jev_allow`, `test_property_an_unlisted_tool_can_never_be_allowed` |
| A server misdescribing its own tools | only host-allowlisted tools are included; hints may only *raise* risk and never set `idempotent` | `test_server_hints_can_only_raise_risk` |
| Irreversible action without approval | `review`, decided before Jev, in every gate mode; only a literal `approved=True` counts | `test_only_a_literal_true_counts_as_approval`, `test_observe_mode_records_...` |
| Cross-user state leakage | per-conversation sessions and stream ids; no state shared between them | `test_sessions_do_not_leak_state_into_each_other` |
| Replay of a stale decision | a newer event on the stream supersedes older in-flight decisions; sequence numbers on every decision | `test_a_newer_event_supersedes_an_older_in_flight_decision` |
| A late response applied to a new event | late results are recorded, never applied | `test_a_late_result_is_recorded_but_never_applied` |
| Probability outside 0..1, or a malformed answer | rejected, never clamped; a malformed answer is an error decision | `test_noul_out_of_range_is_rejected_not_clamped`, `test_a_malformed_response_is_an_error_not_a_decision` |
| Provider outage | deadline, circuit breaker, fail-safe by risk tier; irreversible cannot be configured to allow | `test_circuit_breaker_opens_then_recovers_through_a_probe`, `test_an_irreversible_tier_can_never_be_configured_to_auto_allow` |
| A policy that defaults to allow | rule chains default to `review`; a policy returning an invalid decision degrades to `review` | `test_a_rule_chain_never_defaults_to_allow`, `test_property_allow_only_comes_from_one_specific_combination` |
| Secrets in state, logs or records | redaction before every provider call and write; the API key scrubbed wherever it appears; SDK body logging pinned off; errors never include bodies | `tests/test_redaction.py`, `test_neither_key_nor_state_leaks_into_logs_or_errors` |
| Raw state or goal text persisted | default persists a digest only; goal and raw payload are opt-in | `test_state_is_not_persisted_by_default_only_a_digest` |
| A compromised or hostile MCP server (gateway) | only allowlisted tools are exposed; arguments validated against the downstream schema; instruction-like descriptions withheld; results are judged as context for the next call, not obeyed; downstream errors never reach the model raw | `test_only_allowlisted_tools_are_listed_and_callable`, `test_a_poisoned_tool_description_is_withheld_before_the_model_sees_it`, `test_an_injected_tool_result_holds_back_the_next_call` |
| A model granting itself approval or permission (gateway) | permissions, denials, amount limits and the goal come from host config; approval only from the human via elicitation | `test_approval_can_never_be_claimed_in_the_arguments_or_the_meta`, `test_a_call_without_the_required_permission_never_reaches_the_server` |
| A forged, replayed or tampered approval (gateway) | the answer counts only with request state the gateway sealed for that exact call (session, tool, arguments) | `test_a_bare_approval_answer_with_no_sealed_state_is_ignored_and_the_user_is_asked`, `test_an_approval_for_one_call_cannot_be_replayed_onto_another`, `test_tampered_request_state_is_rejected_by_the_sdk_boundary` |
| An exposed HTTP gateway | loopback-only unless `--allow-remote`, which needs a bearer token; DNS-rebinding protection; stdout never written in stdio mode | `test_a_non_loopback_listen_address_is_refused_without_allow_remote`, `test_a_bearer_token_is_enforced_on_the_wire`, `test_a_foreign_host_header_is_rejected_by_dns_rebinding_protection` |
| Phone-home / telemetry | no core module imports a network library; only the TypeSafe adapter (and its SDK) and the MCP gateway's connector to a downstream URL *you* configured can reach the network | `test_no_core_module_can_phone_home` |

## Hardening checklist for deployers

- [ ] Start in `observe` mode; label decisions; tighten only from your own data.
- [ ] Give every tool a `risk` tier, mark irreversible tools `irreversible`, and set amount
      limits for anything that moves money.
- [ ] Use `ReactorConfig.state_allowlist` so only named fields leave the process.
- [ ] Keep `TYPESAFE_API_KEY` in the environment or a secret manager; never in `.env` files you
      commit, never in `metadata`.
- [ ] Do not enable `TYPESAFE_LOG_LEVEL=debug` in production (the SDK then logs your state).
- [ ] Pin `TYPESAFE_DEFAULT_MODEL` to a versioned id once thresholds are tuned.
- [ ] Keep decision logs private; they contain TypeSafe Output and must not be used to train or
      distill a model.
- [ ] Run the agent under OS-level isolation. This library is a gate, not a sandbox.
- [ ] MCP gateway: list only the tools the task needs, set `goal`, keep irreversible tools
      `irreversible` with `approvals: {mode: elicit}` (or `deny`), and use
      `description_mode: host` for servers you do not control. Over HTTP, keep loopback, or put
      real authentication in front and set `require_session_key: true`.

## Data handling

State you send is processed by TypeSafe. See their Data Processing Agreement and Privacy Policy
(linked from https://docs.typesafe.ai/legal). Zero data retention is offered to enterprise
customers. This project does not upload anything and contains no telemetry.

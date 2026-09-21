# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) once 1.0 is reached (until then, minor versions may
change the API).

## [0.2.0] - 2026-09-21

First release on PyPI. (0.1.0 was only ever tagged in Git and was never published.)

### Added

- **MCP gateway** (`jev-reactor-mcp`, also `jev-reactor mcp ...`; install `jev-reactor[mcp]`):
  a real MCP server, built on the official SDK v2, that fronts other MCP servers (stdio or
  Streamable HTTP) and decides every `tools/call` before forwarding it. A held-back call never
  reaches the downstream server and returns an `isError` result with a plain reason.
  - allowlist (unlisted tools are neither listed nor callable), argument validation against the
    downstream JSON Schema, hard rules, Jev, then policy; `observe` / `guard` / `enforce` modes;
  - approval for irreversible actions through MCP elicitation on both the 2026-07-28 revision
    (input-required results with sealed, per-call request state) and the handshake revisions;
    approval is never read from arguments or `_meta`;
  - description quarantine (`quarantine` / `hide` / `allow`), truncation and host-written
    descriptions; server hints can only raise a risk tier;
  - per-conversation sessions (`_meta["io.jev-reactor/session"]`, `Mcp-Session-Id`);
  - HTTP serving on loopback by default, DNS-rebinding protection, and a bearer token that is
    mandatory for `--allow-remote`;
  - `jev-reactor-mcp init | check | serve`, a safe demo downstream server, `docs/mcp.md`.
- Tests over the real protocol: in-memory client to gateway to demo server, a real stdio
  subprocess, and a real HTTP listener.
- Release plumbing: PEP 639 license metadata, PyPI-safe README links, a trusted-publishing
  workflow.

### Changed

- `GateSession` keeps a bounded history (`history_max`, default 50) instead of growing forever.

### Notes

- The MCP SDK is an optional extra; `import jev_reactor` does not import it (tested).
- Live Jev has not been exercised by this project's automated tests or CI.
- Not included: advisory MCP decision tools, and proxying of downstream resources, prompts,
  sampling and server-initiated elicitation. See `docs/mcp.md`.

## [0.1.0] - 2026-09-21 (Git only, never published to PyPI)

First version: a local runtime and developer CLI.

### Added

- **Reactor**: `decide()` and a streaming `run()` with ordered results, bounded queue
  (backpressure), bounded concurrency, stale-event superseding, late-result recording and clean
  cancellation. One provider request carries every question.
- **Fail-safe by design**: hard rules before Jev and before state building; deadlines enforced
  by the Reactor as well as the provider; a circuit breaker; per-risk-tier failure modes
  (`irreversible` can never be configured to allow).
- **Data model** (Pydantic v2) with Jev's real answer shapes: Noul without confidence, Score
  levels numbered from 0 with string-normalised keys, rounding-aware probability checks, and
  rejection (never clamping) of out-of-range values.
- **Providers**: the TypeSafe adapter (the only module importing `typesafe-sdk`; SDK retry budget
  bounded by the deadline; every SDK error translated; request id optional; SDK body logging
  pinned off), and a deterministic mock provider.
- **Packs and policies**: `tool-loop`, `context-retention`, `agent-loop`; two-sided Noul bands,
  weakest-link confidence, first-match rule chains; a suppression budget; escalation on Noul/
  Choice disagreement; exact-duplicate detection in code; pins and "when in doubt, keep it"
  for context.
- **Question-pack format** (YAML), a linter that encodes TypeSafe's guidance and independent
  measurements (abstain option on every Choice, no bare-number Score levels, declared state
  paths, no string-spliced instructions), and per-pack fingerprints recorded on every event.
- **Redaction and privacy**: masking before every provider call and write, size caps, a field
  allowlist, digest-only persistence by default, opt-in goal and raw-payload persistence.
- **Replay and reports**: recompute policy over recorded answers with no provider; labels in a
  sidecar file; accuracy and calibration only for labelled events, with a small-sample warning;
  warnings for mixed model versions and changed question wording.
- **MCP-compatible tool gate** with `observe` / `guard` / `enforce` modes in which hard rules
  bind, plus a labelled in-memory demo server.
- **CLI**: `init`, `run`, `replay`, `report`, `validate-pack`, `inspect`, `export-pack`,
  `label`, `bench`. No telemetry.
- Examples that run offline with `--mock`, docs (with executed snippets), `SECURITY.md`, CI.

### Notes

- Departures from the original brief, each with its source, are listed in
  [docs/design-notes.md](docs/design-notes.md). Notably: no "Jev Distill" pipeline (TypeSafe's
  terms prohibit training on Jev output), `httpx2.MockTransport` instead of `respx`, and a
  digest-only default for persisted state.
- Was installable from the repository only.

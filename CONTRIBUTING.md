# Contributing

Thanks for helping. This project is deliberately small: it should stay easy to read and
reason about, and its safety properties should stay easy to test.

## Setup

```bash
git clone https://github.com/KNambiarDJsc/Jev-Reactor
cd Jev-Reactor
uv sync --extra dev          # or: pip install -e ".[dev]"
make check                   # ruff, mypy (strict), pytest
```

Python 3.11 or newer. No API key is needed for anything in the default workflow: tests and
examples run offline (`--mock`, `MockProvider`).

## Before you open a pull request

- `make check` passes (lint, strict typing, the full test suite).
- New behaviour has a test. Policy logic is tested with hand-built responses (no provider);
  provider logic is tested through the real SDK against `httpx2.MockTransport`.
- Docs change with the code. The README snippet and every `# runnable` snippet in `docs/` are
  executed by `tests/test_docs.py`, and every `jev-reactor <command>` the docs mention must
  exist.
- `packs/*.yaml` are generated from the Python packs: after changing a pack run
  `jev-reactor export-pack all --out packs` (a test fails on drift). Regenerate fixtures and the
  sample run with `python scripts/gen_fixtures.py` and `python scripts/gen_replay.py`.

## Invariants a change must not break

These are what the tests defend. A pull request that weakens one needs a very good reason.

1. **Hard rules run before Jev and cannot be overridden by any Jev answer or gate mode.**
2. **Nothing defaults to allow.** A rule chain falls through to `review`; an unavailable
   provider never auto-allows an irreversible action; an invalid policy decision degrades to
   `review`.
3. **The Reactor never executes anything.** Only the host does.
4. **Out-of-range or malformed answers are rejected, never clamped.**
5. **State and secrets stay out of logs, errors and default records.**
6. **No core module opens a network connection.** Only the TypeSafe adapter and its SDK do.
7. **Replay never calls a provider.**

## Question packs

A new or changed pack must pass `jev-reactor validate-pack` with no errors, keep instructions
static (untrusted text goes in state fields), include an abstain option on every Choice, and
say in its docstring where its questions and thresholds came from. Thresholds are initial
defaults; do not describe them as calibrated.

## What will not be accepted

- Performance, accuracy or "never hallucinates" claims about Jev, in docs or code, that this
  repository did not itself measure and that TypeSafe's terms allow you to publish. Measure
  with `jev-reactor bench --live` and keep the numbers private.
- Any feature that exports Jev output for training, distillation or imitation. TypeSafe's
  Master Customer Agreement (section 2.3(b)) prohibits it.
- Live network calls in the default test run, or tests that probe TypeSafe's service.
- Telemetry, phone-home, or automatic uploads of any kind.
- A database, Docker, Redis, Kubernetes or cloud deployment in the core package.

## Style

`ruff` for lint and formatting, `mypy --strict` for types. Match the surrounding code: short
modules, docstrings that explain *why* (with the source when a rule came from TypeSafe's docs
or a community measurement), and reason codes that are stable snake_case strings.

## Commits and licensing

Write a clear commit message that says why. By contributing you agree your contribution is
licensed under Apache-2.0, like the rest of the project.

# Releasing

PyPI versions are immutable: a version can be yanked but never re-uploaded or edited. Check
everything before publishing.

## Every release

1. Update the version in `pyproject.toml` and `src/jev_reactor/__init__.py` (a test keeps them
   equal) and add a `## [x.y.z]` section to `CHANGELOG.md` (a test requires it).
2. `uv run ruff check . && uv run mypy && uv run pytest -q`
3. `uv build && uvx twine check dist/*`
4. Smoke-test the wheel in a clean virtualenv (`pip install "dist/<wheel>[mcp]"`, then
   `jev-reactor-mcp init --demo -o g.yaml && jev-reactor-mcp check g.yaml`).
5. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`. The `Release` workflow verifies
   that the tag matches the package version, tests, builds and publishes.

## One-time setup: trusted publishing (recommended)

Trusted publishing lets GitHub Actions publish without a stored API token.

1. On PyPI, open the project, then **Manage, Publishing**, and add a GitHub publisher:
   owner `KNambiarDJsc`, repository `Jev-Reactor`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repository, create an environment named `pypi` (optionally with required
   reviewers).

## API tokens

If you publish by hand with an API token (`uv publish` with `UV_PUBLISH_TOKEN`), prefer a token
scoped to this one project rather than an account-wide token, keep it in an environment
variable or secret store, never in a file in the repository, and revoke it if it is ever pasted
somewhere it should not be.

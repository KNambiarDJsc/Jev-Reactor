.PHONY: install lint type test check clean
install:
	uv sync --extra dev
lint:
	uv run ruff check . && uv run ruff format --check .
type:
	uv run mypy
test:
	uv run pytest
check: lint type test
clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache .hypothesis

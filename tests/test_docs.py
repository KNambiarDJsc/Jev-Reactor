"""The documentation is tested: runnable snippets run, links resolve, commands exist."""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest
from typer.main import get_command

from jev_reactor.cli import app

ROOT = Path(__file__).resolve().parent.parent
DOCS = [
    ROOT / "README.md",
    ROOT / "SECURITY.md",
    ROOT / "CONTRIBUTING.md",
    *sorted((ROOT / "docs").glob("*.md")),
]
DOCS = [p for p in DOCS if p.exists()]
FENCE = re.compile(r"```(\w+)?\n(.*?)```", re.S)


def blocks(path: Path) -> list[tuple[str, str]]:
    return [(lang or "", body) for lang, body in FENCE.findall(path.read_text(encoding="utf-8"))]


RUNNABLE = [
    pytest.param(path, body, id=f"{path.name}#{i}")
    for path in DOCS
    for i, (lang, body) in enumerate(blocks(path))
    if lang == "python" and body.lstrip().startswith("# runnable")
]


@pytest.mark.parametrize(("path", "code"), RUNNABLE)
def test_runnable_snippets_execute(path: Path, code: str) -> None:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, str(path), "exec"), {"__name__": "__docs__"})  # noqa: S102
    printed = buffer.getvalue()
    # a snippet documents its own output in `# -> ...` comments; check they are true
    for expected in re.findall(r"# -> (.+)", code):
        assert expected.strip() in printed, (
            f"{path.name} claims `{expected}` but printed:\n{printed}"
        )


def test_the_readme_has_at_least_one_runnable_snippet() -> None:
    assert any(p.values[0] == ROOT / "README.md" for p in RUNNABLE)


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    broken = []
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if re.match(r"^[a-z]+:", target):  # http:, https:, mailto:
            continue
        if not (path.parent / target).exists():
            broken.append(target)
    assert broken == [], f"{path.name} links to missing files: {broken}"


COMMANDS = set(get_command(app).commands)  # type: ignore[attr-defined]


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_every_cli_command_the_docs_mention_exists(path: Path) -> None:
    mentioned = set(
        re.findall(
            r"^\s*(?:\$ )?jev-reactor (?!--)([a-z][a-z-]*)", path.read_text(encoding="utf-8"), re.M
        )
    )
    assert mentioned <= COMMANDS, f"{path.name} mentions unknown commands: {mentioned - COMMANDS}"


def test_the_readme_makes_no_performance_or_accuracy_claims() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    # numbers with units, and comparative claims, are what a performance claim looks like
    assert not re.search(
        r"\b\d+(\.\d+)?\s?(ms|milliseconds|x faster|x cheaper|% accura)", text.replace("5 ms", "")
    )
    for phrase in (
        "hallucination-proof",
        "never hallucinat",
        "always correct",
        "cannot hallucinate",
    ):
        assert phrase not in text

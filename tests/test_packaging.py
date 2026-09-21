"""Release hygiene: things that are cheap to check here and painful to discover on PyPI."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from jev_reactor import __version__

ROOT = Path(__file__).resolve().parent.parent


def pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_package_version_matches_pyproject() -> None:
    assert pyproject()["project"]["version"] == __version__


def test_the_changelog_has_an_entry_for_this_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in changelog


def test_metadata_uses_an_spdx_license_expression_and_ships_the_notices() -> None:
    project = pyproject()["project"]
    assert project["license"] == "Apache-2.0"
    assert set(project["license-files"]) >= {"LICENSE", "NOTICE"}
    assert not any(c.startswith("License ::") for c in project["classifiers"]), (
        "PEP 639: a License classifier alongside a license expression is rejected by new tooling"
    )


def test_the_readme_is_built_for_pypi_with_absolute_links() -> None:
    config = pyproject()
    assert "readme" in config["project"]["dynamic"]
    hook = config["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
    (sub,) = hook["substitutions"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    rewritten = re.sub(sub["pattern"], sub["replacement"].replace("\\1", r"\1"), readme)
    # every markdown link target is absolute, an anchor or a mailto once the hook has run
    targets = re.findall(r"\]\(([^)\s]+)\)", rewritten)
    relative = [t for t in targets if not t.startswith(("https://", "http://", "#", "mailto:"))]
    assert relative == []
    assert "(https://github.com/KNambiarDJsc/Jev-Reactor/blob/main/docs/mcp.md)" in rewritten


def test_the_mcp_dependencies_are_an_extra_not_a_core_requirement() -> None:
    project = pyproject()["project"]
    assert not any(d.startswith("mcp") for d in project["dependencies"])
    assert any(d.startswith("mcp>=") for d in project["optional-dependencies"]["mcp"])
    assert project["scripts"]["jev-reactor-mcp"] == "jev_reactor.mcp_entry:main"


def test_the_readme_makes_no_claim_that_live_jev_was_measured() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Live Jev has not been exercised" in readme


def test_a_missing_mcp_extra_is_one_clear_line_not_a_traceback() -> None:
    import subprocess
    import sys

    code = "import sys; sys.modules['mcp'] = None;from jev_reactor.mcp_entry import main; main()"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 2 and "Traceback" not in done.stderr
    assert "jev-reactor[mcp]" in done.stderr


def test_the_cli_error_hint_keeps_its_brackets() -> None:
    import sys

    from typer.testing import CliRunner

    from jev_reactor.cli import app

    saved = {
        k: v for k, v in sys.modules.items() if k.startswith(("mcp", "jev_reactor.mcp_server"))
    }
    try:
        for name in list(saved):
            del sys.modules[name]
        sys.modules["mcp"] = None  # type: ignore[assignment]
        result = CliRunner().invoke(app, ["mcp", "check", "x.yaml"])
    finally:
        for name in [k for k in sys.modules if k.startswith(("mcp", "jev_reactor.mcp_server"))]:
            del sys.modules[name]
        sys.modules.update(saved)
    assert result.exit_code == 2
    assert "pip install 'jev-reactor[mcp]'" in result.output

"""Regenerate docs/dependency-notices.md from installed package metadata.

    uv run python scripts/gen_notices.py

Walks the *runtime* requirement graph of jev-reactor (no extras) and lists each package with
its version and license as declared in its own metadata. Licenses are whatever each package
declares; verify against the package if it matters for your use.
"""

from __future__ import annotations

import importlib.metadata as md
import re
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "dependency-notices.md"
ROOT_DIST = "jev-reactor"


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(spec: str) -> str | None:
    """Name of a requirement, or None if it only applies to an extra."""
    requirement, _, marker = spec.partition(";")
    if "extra" in marker:
        return None
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return canonical(match.group(1)) if match else None


def license_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    classifiers = [c for c in classifiers if c and c != "OSI Approved"]
    if classifiers:
        return ", ".join(classifiers)
    text = (meta.get("License") or "").strip().splitlines()
    return text[0][:80] if text and text[0] else "not declared"


def home_page(dist: md.Distribution) -> str:
    meta = dist.metadata
    url = meta.get("Home-page")
    if url:
        return url
    for entry in meta.get_all("Project-URL") or []:
        label, _, target = entry.partition(",")
        if label.strip().lower() in {"homepage", "home", "source", "repository"}:
            return target.strip()
    return ""


def closure() -> dict[str, md.Distribution]:
    seen: dict[str, md.Distribution] = {}
    stack = [ROOT_DIST]
    while stack:
        name = canonical(stack.pop())
        if name in seen:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue  # a platform-specific requirement that is not installed here
        seen[name] = dist
        for spec in dist.requires or []:
            dep = requirement_name(spec)
            if dep:
                stack.append(dep)
    return seen


def main() -> None:
    deps = closure()
    deps.pop(canonical(ROOT_DIST), None)
    rows = [
        f"| {d.metadata['Name']} | {d.version} | {license_of(d)} | {home_page(d)} |"
        for _, d in sorted(deps.items())
    ]
    OUT.write_text(
        "\n".join(
            [
                "# Dependency notices",
                "",
                "Jev Reactor is licensed under Apache-2.0 (see `LICENSE`). At runtime it depends on",
                "the packages below (its own requirements and theirs, resolved on the machine that",
                f"generated this file, {date.today().isoformat()}). Each license is what the package declares",
                "in its own metadata; verify against the package if it matters for your use.",
                "",
                "Development-only tools (pytest, ruff, mypy, hypothesis, pip-audit) are not runtime",
                "dependencies and are not listed. Regenerate with `python scripts/gen_notices.py`.",
                "",
                "| Package | Version | License | Home page |",
                "|---|---|---|---|",
                *rows,
                "",
                '"Jev" and "TypeSafe" are names of TypeSafe AI\'s products. The `typesafe-sdk` package',
                "is TypeSafe's official client; this project is independent and not affiliated with",
                "TypeSafe AI. Use of the TypeSafe API is governed by TypeSafe's own terms.",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {OUT} ({len(rows)} packages)")


if __name__ == "__main__":
    main()

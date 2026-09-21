"""Decide which context items to keep, shorten, or drop. One item per request.

    python examples/context_compaction.py --mock
    python examples/context_compaction.py            # live Jev (needs TYPESAFE_API_KEY)

Jev only judges; it cannot rewrite text. So the output is a *plan*: retain, compact (the host
may summarise) or discard (the host should still keep the original so a wrong drop is
recoverable). Recent turns, system messages and anything that looks like a failure are pinned
in code and never sent to Jev at all. When in doubt, the item is kept.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from rich.console import Console
from rich.table import Table

from jev_reactor import Reactor, ReactorConfig
from jev_reactor.demo import fixtures_dir, is_mock_requested
from jev_reactor.packs import ContextPolicy, classify_items, context_pack
from jev_reactor.providers.mock import MockProvider


async def main(mock: bool) -> None:
    console = Console()
    data = json.loads((fixtures_dir() / "context_messages.json").read_text(encoding="utf-8"))
    by_id = {e["item"]["id"]: e for e in data["items"]}

    config = ReactorConfig(deadline_seconds=10.0 if not is_mock_requested(mock or None) else 1.0)
    if is_mock_requested(mock or None):
        provider: Any = MockProvider(
            respond=lambda state, _q: by_id[state["item"]["id"]]["answers"]
        )
    else:
        from jev_reactor.providers.typesafe import TypeSafeProvider

        provider = TypeSafeProvider()

    async with Reactor(
        provider, pack=context_pack(), policy=ContextPolicy(), config=config
    ) as reactor:
        items = [e["item"] for e in data["items"]]
        plan = await classify_items(
            reactor, items, goal=data["goal"], agent_context=data.get("agent_context")
        )

    table = Table(title=f"Goal: {data['goal']}")
    for column in ("item", "role", "age", "action", "why", "text"):
        table.add_column(column, overflow="fold")
    for item in items:
        d = plan.decisions[item["id"]]
        table.add_row(
            item["id"],
            item["role"],
            str(item["age_turns"]),
            d.target or d.action,
            ", ".join(d.reason_codes),
            item["text"].replace("\n", " ")[:46] + ("..." if len(item["text"]) > 46 else ""),
        )
    console.print(table)
    console.print(
        f"\nretain {len(plan.retain)}   compact {len(plan.compact)}   discard {len(plan.discard)}"
        f"   (compaction rate {plan.compaction_rate:.0%})"
    )
    pinned = [i for i, d in plan.decisions.items() if d.provider_status == "not_called"]
    console.print(f"Pinned in code, never sent to Jev: {pinned}")
    console.print(
        "[dim]This is a plan. Nothing was deleted; store dropped originals so they can be recalled.[/]"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])  # type: ignore[union-attr]
    parser.add_argument("--mock", action="store_true", help="use recorded answers")
    asyncio.run(main(parser.parse_args().mock))

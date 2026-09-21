"""Tool-loop decisions in a host loop: only ``allow`` ever reaches the tool.

    python examples/tool_loop.py --mock      # recorded answers, no key, no network
    python examples/tool_loop.py             # live Jev (needs TYPESAFE_API_KEY)

Eight scenarios stream through ``Reactor.run``. The host below is the only code that
executes anything, and it does so for ``allow`` alone: a skipped, reviewed, blocked or
stopped call never touches the (fake, in-memory) tool server.

Every decision is also written to ./decisions/tool_loop.jsonl so you can inspect, report and
replay it afterwards:

    jev-reactor report decisions/tool_loop.jsonl
    jev-reactor replay decisions/tool_loop.jsonl --policy strict --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console
from rich.table import Table

from jev_reactor import JsonlSink, Reactor, ReactorConfig
from jev_reactor.adapters import FakeToolServer, ToolCall
from jev_reactor.demo import demo_loop, event_from_fixture, load_scenarios, make_provider

LOG = Path("decisions") / "tool_loop.jsonl"


async def main(mock: bool) -> None:
    console = Console()
    loop = demo_loop()
    scenarios = load_scenarios()
    config = ReactorConfig(
        deadline_seconds=10.0 if not mock else 1.0,
        persist_state="redacted",  # lets hard rules such as exact duplicates be replayed later
    )
    provider = make_provider(mock or None, scenarios, loop.pack, config)
    server = FakeToolServer()  # DEMO: in-memory, no side effects
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text("", encoding="utf-8")  # a fresh log per run

    async with Reactor(
        provider, pack=loop.pack, policy=loop.policy, config=config, sinks=[JsonlSink(LOG)]
    ) as reactor:
        events = []
        for scenario in scenarios:
            event = event_from_fixture(reactor, scenario)
            # one stream per scenario, so newer events do not supersede these older ones
            event.metadata["stream_id"] = scenario.name
            events.append((scenario, event))

        table = Table(title="Tool-loop decisions")
        for column in ("scenario", "decision", "reason", "executed?"):
            table.add_column(column, overflow="fold")
        results = reactor.run([e for _, e in events])
        async for decision in results:
            scenario, event = next((s, e) for s, e in events if e.event_id == decision.event_id)
            call = event.proposed_action or {}
            executed = "no"
            if decision.action == "allow":  # the ONLY path to execution
                await server.call(ToolCall(name=call["tool"], arguments=call.get("arguments", {})))
                executed = f"yes: {call['tool']}"
            table.add_row(
                scenario.name, decision.action, ", ".join(decision.reason_codes), executed
            )
        console.print(table)

    console.print(f"\nExecuted by the demo tool server: {[c.name for c in server.executed]}")
    console.print(f"Emails actually sent: 0  (outbox holds {len(server.outbox)} demo entries)")
    console.print(f"Decisions logged to {LOG}")
    if mock:
        console.print("[dim]Mock mode: answers were recorded, not produced by Jev just now.[/]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])  # type: ignore[union-attr]
    parser.add_argument("--mock", action="store_true", help="use recorded answers")
    asyncio.run(main(parser.parse_args().mock))

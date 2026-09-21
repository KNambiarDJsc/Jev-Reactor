"""Regenerate examples/replay.jsonl deterministically from the recorded scenarios.

Fixed event ids, timestamps and a fake clock make the file byte-stable, so regenerating it
produces no noisy diff:

    python scripts/gen_replay.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jev_reactor import JsonlSink, MockProvider, Reactor, ReactorConfig, ReactorEvent
from jev_reactor.demo import ScenarioProvider, demo_loop, load_scenarios

OUT = Path(__file__).resolve().parent.parent / "examples" / "replay.jsonl"
START = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

#: relevant/useful, but not overwhelmingly: allowed by default, sent to review by "strict"
BORDERLINE = {
    "relevant": 0.75,
    "should_call": 0.75,
    "redundant": 0.05,
    "task_complete": 0.05,
    "needs_user_input": 0.05,
    "injection_suspected": 0.02,
    "next_action": ("call_tool", 0.90),
}


class TickClock:
    """A clock that advances 4 ms per reading, so recorded latencies are reproducible."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.004
        return self.now


async def main() -> None:
    loop = demo_loop()
    scenarios = load_scenarios()
    config = ReactorConfig(persist_state="redacted")
    OUT.write_text("", encoding="utf-8")
    sink = JsonlSink(OUT)

    events: list[tuple[ReactorEvent, str]] = []
    for n, sc in enumerate(scenarios, start=1):
        events.append(
            (
                ReactorEvent(
                    event_id=f"evt_demo_{n:02d}",
                    sequence=n,
                    timestamp=START + timedelta(seconds=n),
                    **sc.event,
                ),
                sc.name,
            )
        )

    reactor = Reactor(
        ScenarioProvider(scenarios, loop.pack, config),
        pack=loop.pack,
        policy=loop.policy,
        config=config,
        sinks=[sink],
        clock=TickClock(),
    )
    for event, _ in events:
        await reactor.decide(event)

    base = scenarios[[s.name for s in scenarios].index("safe_tool_call")].event
    borderline = ReactorEvent(
        event_id=f"evt_demo_{len(events) + 1:02d}",
        sequence=len(events) + 1,
        timestamp=START + timedelta(seconds=len(events) + 1),
        **{**base, "goal": "Find the current status of invoice INV-2045",
           "proposed_action": {"tool": "search_invoices", "arguments": {"invoice_id": "INV-2045"}}},
    )  # fmt: skip
    borderline_reactor = Reactor(
        MockProvider(BORDERLINE, model="jev-1.13.0", reported_latency_ms=88.0),
        pack=loop.pack,
        policy=loop.policy,
        config=config,
        sinks=[sink],
        clock=TickClock(),
    )
    await borderline_reactor.decide(borderline)
    sink.close()
    print(f"wrote {OUT} ({len(events) + 1} events)")


if __name__ == "__main__":
    asyncio.run(main())

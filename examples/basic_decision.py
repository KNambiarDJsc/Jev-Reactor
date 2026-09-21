"""One event in, one decision out.

    python examples/basic_decision.py --mock      # recorded answers, no key, no network
    python examples/basic_decision.py             # live Jev (needs TYPESAFE_API_KEY)

The event is an agent proposing a lookup it may already have the answer to. Jev answers
seven small typed questions in a single request; ordinary Python policy turns those answers
into an action. Nothing is executed here: the Reactor only returns a decision.
"""

from __future__ import annotations

import argparse
import asyncio

from jev_reactor import Reactor, ReactorConfig
from jev_reactor.demo import demo_loop, event_from_fixture, load_scenarios, make_provider


async def main(mock: bool) -> None:
    loop = demo_loop()
    (scenario,) = load_scenarios(["redundant_tool_call"])
    config = ReactorConfig(deadline_seconds=10.0 if not mock else 1.0)
    provider = make_provider(mock or None, [scenario], loop.pack, config)

    async with Reactor(provider, pack=loop.pack, policy=loop.policy, config=config) as reactor:
        event = event_from_fixture(reactor, scenario)
        print(f"Goal:     {event.goal}")
        print(f"Proposed: {event.proposed_action['tool']}({event.proposed_action['arguments']})")  # type: ignore[index]
        print()

        record = await reactor.decide_record(event)
        decision = record.policy_result
        print(f"Decision: {decision.action}")
        print(f"Reason: {', '.join(decision.reason_codes)}")
        if record.response is not None:
            signals = decision.metadata.get("signals", {})
            if "redundant" in signals:
                print(f"Redundancy probability: {signals['redundant']:.2f}")
            choice = record.response.answers["next_action"]
            print(f"Choice confidence: {choice.confidence:.2f}")
            print(f"\nModel: {record.response.model}  |  request id: {record.response.request_id}")
        print(f"Decided by rule: {decision.rule}  |  provider status: {decision.provider_status}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])  # type: ignore[union-attr]
    parser.add_argument("--mock", action="store_true", help="use recorded answers")
    asyncio.run(main(parser.parse_args().mock))

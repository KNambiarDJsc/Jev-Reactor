"""A tool gate in front of a (fake) MCP-style server. A held-back call is never executed.

    python examples/mcp_proxy.py --mock                 # recorded answers
    python examples/mcp_proxy.py --mock --mode observe  # log what WOULD happen, block nothing
    python examples/mcp_proxy.py                        # live Jev (needs TYPESAFE_API_KEY)

DEMO ONLY. ``FakeToolServer`` is an in-memory stand-in: no network, no files, no real
side effects. It is not an MCP server. The point is the gate: every proposed call goes through
the Reactor first, and the host runs it only if the verdict allows.

Modes:  enforce (default) runs only ``allow``.  guard also runs Jev's steering verdicts.
observe runs everything Jev disagrees with too, but your hard rules (allowlist,
permissions, denials, amount limits, approval for irreversible actions) bind in *every* mode.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from rich.console import Console
from rich.table import Table

from jev_reactor import Reactor, ReactorConfig
from jev_reactor.adapters import FakeToolServer, ToolGate, tools_from_mcp
from jev_reactor.demo import is_mock_requested
from jev_reactor.providers.mock import MockProvider

# The host's allowlist. The server's own list of tools is NOT trusted on its own.
ALLOW: dict[str, Any] = {
    "search_invoices": {"risk": "read", "idempotent": True, "requires_permission": "invoices:read"},
    "get_invoice_status": {"risk": "read", "requires_permission": "invoices:read"},
    "send_email": {"risk": "irreversible", "requires_permission": "email:send"},
    "refund_payment": {
        "risk": "irreversible",
        "requires_permission": "payments:refund",
        "amount_arg": "amount",
        "max_amount": 500,
    },
}

SAFE = {
    "relevant": 0.96,
    "should_call": 0.94,
    "redundant": 0.04,
    "task_complete": 0.03,
    "needs_user_input": 0.02,
    "injection_suspected": 0.01,
    "next_action": ("call_tool", 0.91),
}
REDUNDANT = {**SAFE, "should_call": 0.10, "redundant": 0.93, "next_action": ("skip_tool", 0.9)}


def recorded_answers(state: Any, _questions: Any) -> Any:
    """Mock mode: a call to get_invoice_status after a search is a semantic repeat."""
    tool = state["proposed_call"]["tool"]
    return REDUNDANT if tool == "get_invoice_status" and state["recent_calls"] else SAFE


async def main(mock: bool, mode: str) -> None:
    console = Console()
    server = FakeToolServer()
    tools = tools_from_mcp(server.list_tools(), ALLOW)  # write_note is not allowlisted: dropped
    config = ReactorConfig(deadline_seconds=1.0 if is_mock_requested(mock or None) else 10.0)
    provider: Any
    if is_mock_requested(mock or None):
        provider = MockProvider(respond=recorded_answers)
    else:
        from jev_reactor.providers.typesafe import TypeSafeProvider

        provider = TypeSafeProvider()

    async with Reactor(provider, config=config) as reactor:
        gate = ToolGate(reactor, tools, mode=mode)  # type: ignore[arg-type]
        session = gate.session(
            "Tell the customer whether invoice INV-2041 is paid",
            permissions=["invoices:read", "email:send"],  # note: no payments:refund
            agent_context="Internal finance assistant. Never send money or messages unapproved.",
        )
        steps: list[tuple[str, dict[str, Any], bool, str]] = [
            ("search_invoices", {"invoice_id": "INV-2041"}, False, "a useful first lookup"),
            ("search_invoices", {"invoice_id": "INV-2041"}, False, "the exact same call again"),
            ("get_invoice_status", {"id": "INV-2041"}, False, "the same information, other tool"),
            ("delete_everything", {}, False, "a tool nobody allowlisted"),
            ("refund_payment", {"amount": 5000}, True, "over the limit, and no permission"),
            ("send_email", {"to": "customer@example.com", "body": "Invoice paid."}, False, "irreversible, not approved"),
            ("send_email", {"to": "customer@example.com", "body": "Invoice paid."}, True, "irreversible, explicitly approved"),
        ]  # fmt: skip
        table = Table(title=f"mode: {mode}")
        for column in ("call", "why it is interesting", "decision", "reason", "ran?"):
            table.add_column(column, overflow="fold")
        for name, args, approved, why in steps:
            res = await session.call(name, args, executor=server.call, approved=approved)
            d = res.verdict.decision
            note = (
                ""
                if res.verdict.would_execute_if_enforced == res.executed
                else " (enforce would differ)"
            )
            table.add_row(
                name,
                why,
                d.action,
                ", ".join(d.reason_codes),
                ("yes" if res.executed else "no") + note,
            )
        console.print(table)

    ran = [c.name for c in server.executed]
    console.print(f"\nRan on the demo server: {ran}")
    console.print(
        f"Demo outbox: {len(server.outbox)} email(s) queued, 0 actually sent. Refunds: {len(server.refunds)}."
    )
    # The guarantee this example exists to show:
    assert "delete_everything" not in ran and not server.refunds, "a blocked call was executed!"
    console.print("[green]No blocked or unapproved call was executed.[/]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])  # type: ignore[union-attr]
    parser.add_argument("--mock", action="store_true", help="use recorded answers")
    parser.add_argument("--mode", choices=["observe", "guard", "enforce"], default="enforce")
    args = parser.parse_args()
    asyncio.run(main(args.mock, args.mode))

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jev_reactor.config import ReactorConfig
from jev_reactor.models import ReactorEvent
from jev_reactor.packs import ToolLoop, ToolSpec
from jev_reactor.providers.mock import MockProvider, load_fixture
from jev_reactor.reactor import Reactor

FIXTURES = Path(__file__).parent / "fixtures"
CONTEXT_FIXTURE = FIXTURES / "context_messages.json"
TOOL_FIXTURES = sorted(p for p in FIXTURES.glob("*.json") if p.name != CONTEXT_FIXTURE.name)

TOOLS = [
    ToolSpec(
        name="search_invoices",
        risk="read",
        idempotent=True,
        requires_permission="invoices:read",
        description="Search invoices by id or customer.",
    ),
    ToolSpec(
        name="get_invoice_status",
        risk="read",
        requires_permission="invoices:read",
        description="Return the payment status of one invoice.",
    ),
    ToolSpec(name="write_note", risk="write", description="Attach a note to an invoice."),
    ToolSpec(
        name="send_email",
        risk="irreversible",
        requires_permission="email:send",
        description="Send an email to a customer.",
    ),
    ToolSpec(
        name="refund_payment",
        risk="irreversible",
        requires_permission="payments:refund",
        amount_arg="amount",
        max_amount=500.0,
        description="Refund a payment.",
    ),
]


@pytest.fixture
def tools() -> list[ToolSpec]:
    return list(TOOLS)


@pytest.fixture
def loop() -> ToolLoop:
    return ToolLoop(TOOLS)


def event_from(reactor: Reactor, raw: dict[str, Any]) -> ReactorEvent:
    return reactor.new_event(
        raw["event_type"],
        raw.get("state"),
        goal=raw.get("goal"),
        proposed_action=raw.get("proposed_action"),
        metadata=raw.get("metadata"),
    )


def strict_config(**kw: Any) -> ReactorConfig:
    """Tests want policy bugs to raise instead of degrading to review."""
    return ReactorConfig(strict_policy_errors=True, **kw)


def reactor_for(fixture_path: Path, loop: ToolLoop, **config: Any) -> tuple[Reactor, MockProvider]:
    provider = MockProvider.from_fixture(fixture_path)
    return Reactor(
        provider, pack=loop.pack, policy=loop.policy, config=strict_config(**config)
    ), provider


__all__ = ["CONTEXT_FIXTURE", "FIXTURES", "TOOLS", "TOOL_FIXTURES", "event_from", "load_fixture"]

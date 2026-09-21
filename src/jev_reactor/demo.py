"""Shared scaffolding for the examples and the CLI: safe fake tools and recorded scenarios.

Nothing here talks to a network. The "tools" are in-memory fakes; the mock provider answers
from recorded fixtures.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jev_reactor.config import ReactorConfig
from jev_reactor.errors import ProviderRejectedError, ReactorError
from jev_reactor.models import DecisionResponse, QuestionSpec, ReactorEvent, canonical_json
from jev_reactor.packs import ToolLoop, ToolSpec
from jev_reactor.providers.base import DecisionProvider
from jev_reactor.providers.mock import Fixture, MockProvider, build_answer, load_fixture
from jev_reactor.questions import QuestionPack
from jev_reactor.reactor import Reactor, build_provider_state
from jev_reactor.redaction import Redactor

MOCK_ENV = "JEV_REACTOR_MOCK"

#: the demo allowlist. In a real deployment this is *your* trusted configuration.
DEMO_TOOLS: list[ToolSpec] = [
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


def demo_loop() -> ToolLoop:
    return ToolLoop(DEMO_TOOLS)


def is_mock_requested(flag: bool | None = None) -> bool:
    if flag is not None:
        return flag
    return os.environ.get(MOCK_ENV, "").lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------- data files


def data_dir() -> Path:
    """Packaged data (``_data``) when installed, the repository root in a checkout."""
    packaged = Path(__file__).parent / "_data"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2]


def fixtures_dir() -> Path:
    """``./fixtures`` (after ``jev-reactor init``), else the packaged or checkout fixtures."""
    local = Path.cwd() / "fixtures"
    if (local / "safe_tool_call.json").is_file():
        return local
    base = data_dir()
    for candidate in (base / "fixtures", base / "tests" / "fixtures"):
        if candidate.is_dir():
            return candidate
    raise ReactorError("fixtures not found; run `jev-reactor init` in an empty directory first")


def load_scenarios(names: Sequence[str] | None = None) -> list[Fixture]:
    root = fixtures_dir()
    paths = (
        [root / f"{n}.json" for n in names]
        if names
        else sorted(p for p in root.glob("*.json") if p.stem != "context_messages")
    )
    return [load_fixture(p) for p in paths]


# ---------------------------------------------------------------------------- mock provider


class ScenarioProvider:
    """A mock that answers according to *which recorded scenario the state describes*.

    Each scenario's state is computed exactly as the Reactor computes it (redaction and
    all), so ``Reactor.run`` can stream several different scenarios through one provider.
    """

    def __init__(
        self,
        scenarios: Sequence[Fixture],
        pack: QuestionPack,
        config: ReactorConfig | None = None,
    ) -> None:
        cfg = config or ReactorConfig()
        redactor = Redactor(cfg.redaction)
        probe = Reactor(MockProvider(), pack=pack, policy=None, config=cfg)  # only for new_event
        self._answers: dict[str, dict[str, Any]] = {}
        self._models: dict[str, tuple[str, float]] = {}
        self._names: dict[str, str] = {}
        for fx in scenarios:
            ev = _event_from(probe, fx.event)
            state, _ = build_provider_state(ev, pack, cfg, redactor)
            key = canonical_json(state)
            if key in self._answers:
                other = next(n for n, k in self._names.items() if k == key)
                raise ReactorError(
                    f"scenarios {other!r} and {fx.name!r} produce identical provider state, so a "
                    "state-keyed mock cannot tell them apart; make their goals or arguments differ"
                )
            self._names[fx.name] = key
            self._answers[key] = fx.answers
            self._models[key] = (fx.model, fx.latency_ms)
        self.calls = 0

    async def evaluate(
        self, *, state: Any, questions: dict[str, QuestionSpec], timeout: float
    ) -> DecisionResponse:
        self.calls += 1
        key = canonical_json(state)
        if key not in self._answers:
            raise ProviderRejectedError("no recorded scenario matches this state (mock mode)")
        model, latency = self._models[key]
        answers = {
            qid: build_answer(spec, self._answers[key][qid])
            for qid, spec in questions.items()
            if qid in self._answers[key]
        }
        return DecisionResponse(
            request_id=f"mock-{self.calls}", model=model, answers=answers, latency_ms=latency
        )


def _event_from(reactor: Reactor, raw: dict[str, Any]) -> ReactorEvent:
    return reactor.new_event(
        raw["event_type"],
        raw.get("state"),
        goal=raw.get("goal"),
        proposed_action=raw.get("proposed_action"),
        metadata=raw.get("metadata"),
    )


def event_from_fixture(reactor: Reactor, fixture: Fixture) -> ReactorEvent:
    return _event_from(reactor, fixture.event)


def make_provider(
    mock: bool | None, scenarios: Sequence[Fixture], pack: QuestionPack, config: ReactorConfig
) -> DecisionProvider:
    """Mock (recorded answers) or live Jev. Live needs ``TYPESAFE_API_KEY``."""
    if is_mock_requested(mock):
        return ScenarioProvider(scenarios, pack, config)
    from jev_reactor.providers.typesafe import TypeSafeProvider

    return TypeSafeProvider()

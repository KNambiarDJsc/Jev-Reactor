from __future__ import annotations

import pytest

from conftest import FIXTURES, TOOL_FIXTURES
from jev_reactor.demo import ScenarioProvider, demo_loop, load_scenarios
from jev_reactor.errors import ReactorError
from jev_reactor.providers.mock import load_fixture


def test_every_scenario_produces_its_own_provider_state() -> None:
    """A state-keyed mock is only safe if no two scenarios look identical to it."""
    loop = demo_loop()
    ScenarioProvider(load_scenarios(), loop.pack)  # raises if two scenarios collide


def test_identical_scenarios_are_rejected_instead_of_silently_merged() -> None:
    loop = demo_loop()
    fx = load_fixture(FIXTURES / "safe_tool_call.json")
    twin = load_fixture(FIXTURES / "safe_tool_call.json")
    twin.name = "twin"
    with pytest.raises(ReactorError, match="identical provider state"):
        ScenarioProvider([fx, twin], loop.pack)


def test_fixtures_are_honest_about_being_hand_written() -> None:
    for path in TOOL_FIXTURES:
        fx = load_fixture(path)
        assert fx.model == "mock-fixture", f"{path.name} claims a real model"
        assert fx.latency_ms == 0.0, f"{path.name} carries a made-up latency"

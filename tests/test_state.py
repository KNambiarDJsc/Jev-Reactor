from __future__ import annotations

import pytest

from jev_reactor.models import ReactorEvent
from jev_reactor.state import (
    StateTooLargeError,
    allowlist_state,
    bound_state,
    default_state,
    get_path,
    normalise_path,
    path_declared,
    state_chars,
)


def test_default_state_adds_goal_and_action_without_overriding_the_host() -> None:
    ev = ReactorEvent(
        event_id="e",
        sequence=1,
        timestamp="2026-01-01T00:00:00Z",  # type: ignore[arg-type]
        event_type="t",
        state={"goal": "host goal"},
        goal="event goal",
        proposed_action={"tool": "x"},
    )
    state = default_state(ev)
    assert state["goal"] == "host goal"
    assert state["proposed_action"] == {"tool": "x"}


def test_get_path_handles_keys_and_indexes() -> None:
    obj = {"recent": [{"tool": "a"}, {"tool": "b", "args": {"q": 1}}]}
    assert get_path(obj, "recent[1].args.q") == 1
    with pytest.raises(KeyError):
        get_path(obj, "recent[5].tool")


def test_paths_normalise_indexes_to_wildcards() -> None:
    assert normalise_path("recent_calls[3].tool") == "recent_calls[].tool"


@pytest.mark.parametrize(
    ("reference", "declared", "expected"),
    [
        ("goal", ["goal"], True),
        (
            "proposed_call.arguments.query",
            ["proposed_call.arguments"],
            True,
        ),  # deeper than declared
        ("recent_calls", ["recent_calls[].tool"], True),  # ancestor of declared
        ("recent_calls[0].tool", ["recent_calls[].tool"], True),
        ("recent", ["recent_calls"], False),  # prefix of the *name* is not a path prefix
        ("nope", ["goal"], False),
    ],
)
def test_path_declared(reference: str, declared: list[str], expected: bool) -> None:
    assert path_declared(reference, declared) is expected


def test_allowlist_keeps_only_named_paths() -> None:
    state = {
        "goal": "g",
        "secret_notes": "x",
        "call": {"tool": "t", "arguments": {"a": 1}, "junk": 2},
    }
    out = allowlist_state(state, ["goal", "call.tool", "call.arguments"])
    assert out == {"goal": "g", "call": {"tool": "t", "arguments": {"a": 1}}}
    assert allowlist_state(state, []) == {}


def test_small_state_is_returned_untouched() -> None:
    state = {"goal": "g"}
    assert bound_state(state, max_chars=1000) is state


def test_oldest_history_is_trimmed_first_and_the_input_is_not_mutated() -> None:
    calls = [{"tool": "t", "result": "x" * 50, "n": i} for i in range(10)]
    state = {"goal": "g", "recent_calls": calls}
    limit = state_chars(state) // 2
    out = bound_state(state, max_chars=limit, trim_lists=["recent_calls"])
    assert state_chars(out) <= limit
    kept = [c["n"] for c in out["recent_calls"]]
    assert kept == sorted(kept) and kept[-1] == 9, "newest items must survive"
    assert len(state["recent_calls"]) == 10, "caller's state must not be mutated"


def test_state_that_cannot_fit_raises_instead_of_being_sent() -> None:
    with pytest.raises(StateTooLargeError) as info:
        bound_state({"goal": "x" * 5000}, max_chars=256, trim_lists=["recent_calls"])
    assert info.value.limit == 256

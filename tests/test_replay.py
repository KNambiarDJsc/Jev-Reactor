"""Replay and report: recorded runs are re-decided without any provider or network."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from conftest import TOOL_FIXTURES, event_from, strict_config
from jev_reactor.config import ReactorConfig
from jev_reactor.errors import PolicyError, ProviderUnavailableError, ReplayError
from jev_reactor.metrics import build_report, percentile
from jev_reactor.models import DecisionEvent
from jev_reactor.packs import ToolLoop, ToolLoopPolicy
from jev_reactor.providers.mock import MockProvider, load_fixture
from jev_reactor.providers.typesafe import TypeSafeProvider
from jev_reactor.reactor import Reactor
from jev_reactor.replay import (
    append_label,
    apply_labels,
    labels_path,
    load_events,
    load_labels,
    replay_events,
)
from jev_reactor.sinks import JsonlSink

MIDDLING = {
    "relevant": 0.75,
    "should_call": 0.75,
    "redundant": 0.05,
    "task_complete": 0.05,
    "needs_user_input": 0.05,
    "injection_suspected": 0.02,
    "next_action": ("call_tool", 0.9),
}


async def record(path: Path, loop: ToolLoop, *, extra: bool = True) -> None:
    """Record every tool-loop fixture, plus one borderline case, into one JSONL file."""
    sink = JsonlSink(path)
    for fixture in TOOL_FIXTURES:
        fx = load_fixture(fixture)
        reactor = Reactor(
            MockProvider.from_fixture(fixture),
            pack=loop.pack,
            policy=loop.policy,
            config=strict_config(),
            sinks=[sink],
        )
        await reactor.decide(event_from(reactor, fx.event))
    if extra:
        base = load_fixture(next(p for p in TOOL_FIXTURES if p.stem == "safe_tool_call"))
        reactor = Reactor(
            MockProvider(MIDDLING),
            pack=loop.pack,
            policy=loop.policy,
            config=strict_config(),
            sinks=[sink],
        )
        await reactor.decide(event_from(reactor, base.event))
    sink.close()


@pytest.fixture
async def run_file(tmp_path: Path, loop: ToolLoop) -> Path:
    path = tmp_path / "runs.jsonl"
    await record(path, loop)
    return path


# ---------------------------------------------------------------------------- replay


async def test_replaying_with_the_same_policy_changes_nothing(
    run_file: Path, loop: ToolLoop
) -> None:
    result = replay_events(load_events(run_file).events, ToolLoopPolicy(loop.tools))
    assert len(result.rows) == 9 and result.changed == []
    assert all(not row.note or "kept as recorded" in row.note for row in result.rows)


async def test_a_recorded_run_can_be_replayed_with_a_new_policy(run_file: Path) -> None:
    """Milestone 5 acceptance: new policy, same recorded answers, no call to Jev."""
    strict = ToolLoopPolicy.for_replay("strict")
    result = replay_events(load_events(run_file).events, strict)
    assert result.transitions() == {"allow->review": 1}
    (row,) = result.changed
    assert row.original.action == "allow" and row.replayed.action == "review"
    assert row.replayed.event_id == row.original.event_id, "identity is preserved"


async def test_replay_uses_no_provider_and_no_network(
    run_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = load_events(run_file).events

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("replay must not touch the network or a provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(TypeSafeProvider, "evaluate", forbidden)
    replay_events(events, ToolLoopPolicy.for_replay("default"))
    build_report(events)


async def test_events_decided_by_hard_rules_are_rederived_or_kept(
    run_file: Path, loop: ToolLoop
) -> None:
    events = [e for e in load_events(run_file).events if e.provider_status == "not_called"]
    assert len(events) == 1  # the unsafe_tool_call fixture
    full = replay_events(events, ToolLoopPolicy(loop.tools)).rows[0]
    assert full.replayed.action == "review" and full.note is None, "re-derived from the event"
    thresholds_only = replay_events(events, ToolLoopPolicy.for_replay()).rows[0]
    assert thresholds_only.replayed == thresholds_only.original.model_copy(
        update={"provider_status": "not_called"}
    )
    assert thresholds_only.note and "kept as recorded" in thresholds_only.note


async def test_failure_decisions_follow_the_replay_configuration(
    tmp_path: Path, loop: ToolLoop
) -> None:
    path = tmp_path / "fail.jsonl"
    sink = JsonlSink(path)
    fx = load_fixture(next(p for p in TOOL_FIXTURES if p.stem == "safe_tool_call"))
    reactor = Reactor(
        MockProvider(raises=[ProviderUnavailableError("down")]),
        pack=loop.pack,
        policy=loop.policy,
        config=strict_config(),
        sinks=[sink],
    )
    original = await reactor.decide(event_from(reactor, fx.event))
    sink.close()
    assert original.action == "fallback"  # read tool

    cautious = ReactorConfig(
        failure_by_risk={"read": "review", "write": "review", "irreversible": "block"}
    )
    result = replay_events(load_events(path).events, ToolLoopPolicy(loop.tools), config=cautious)
    assert result.transitions() == {"fallback->review": 1}


async def test_a_policy_that_misbehaves_is_reported_not_swallowed(run_file: Path) -> None:
    class Broken:
        def decide(self, *, event: Any, response: Any) -> Any:
            return {"action": "call_tool", "reason_codes": ["x"]}

    events = load_events(run_file).events
    lenient = replay_events(events, Broken())  # type: ignore[arg-type]
    ok_rows = [r for r in lenient.rows if r.record.provider_status == "ok"]
    assert ok_rows and all(r.replayed.reason_codes == ["policy_error"] for r in ok_rows)
    assert all("invalid decision" in (r.note or "") for r in ok_rows)
    with pytest.raises(PolicyError, match="invalid decision"):
        replay_events(events, Broken(), strict=True)  # type: ignore[arg-type]


async def test_late_records_are_counted_but_never_decided_twice(run_file: Path) -> None:
    events = load_events(run_file).events
    late = events[0].model_copy(update={"provider_status": "late"})
    result = replay_events([*events, late], ToolLoopPolicy.for_replay())
    assert result.skipped_late == 1 and len(result.rows) == 9
    assert build_report([*events, late]).late_records == 1


# ---------------------------------------------------------------------------- loading


def test_a_truncated_final_line_is_tolerated(tmp_path: Path) -> None:
    good = _one_line(tmp_path)
    path = tmp_path / "cut.jsonl"
    path.write_text(good + "\n" + good[: len(good) // 2], encoding="utf-8")
    loaded = load_events(path)
    assert len(loaded.events) == 1
    assert [i.kind for i in loaded.issues] == ["truncated_tail"] and loaded.issues[0].line == 2


def test_a_malformed_line_names_its_line_number(tmp_path: Path) -> None:
    good = _one_line(tmp_path)
    path = tmp_path / "bad.jsonl"
    path.write_text(f'{good}\n{{"not": "an event"}}\n{good}\n\n{good}\n', encoding="utf-8")
    lenient = load_events(path)
    assert len(lenient.events) == 3 and lenient.issues[0].line == 2
    with pytest.raises(ReplayError, match="line 2") as info:
        load_events(path, strict=True)
    assert info.value.line == 2


def test_an_unreadable_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="cannot read"):
        load_events(tmp_path / "missing.jsonl")


def _one_line(tmp_path: Path) -> str:
    sample = Path(__file__).parent / "fixtures" / "safe_tool_call.json"
    data = json.loads(sample.read_text(encoding="utf-8"))
    from datetime import UTC, datetime

    from jev_reactor.models import ActionDecision, ReactorEvent

    rec = DecisionEvent(
        event=ReactorEvent(
            event_id="e1",
            sequence=1,
            timestamp=datetime.now(UTC),
            event_type=data["event"]["event_type"],
            state={},
        ),
        question_pack_id="tool-loop",
        question_pack_version="1.0",
        policy_result=ActionDecision(action="allow", reason_codes=["x"]),
        provider_status="not_called",
    )
    return rec.to_jsonl()


# ---------------------------------------------------------------------------- report


def test_percentile_uses_nearest_rank() -> None:
    values = list(range(1, 101))
    assert percentile(values, 95) == 95 and percentile(values, 50) == 50
    assert percentile([7.0], 95) == 7.0 and percentile([], 95) is None


async def test_report_summarises_only_what_was_recorded(run_file: Path) -> None:
    rep = build_report(load_events(run_file).events)
    assert rep.total == 9
    assert rep.provider_status == {"ok": 8, "not_called": 1}
    assert rep.actions == {"allow": 2, "skip": 1, "review": 4, "stop": 1, "route": 1}
    assert rep.mean_question_count == 7  # every provider call carried all seven questions
    assert rep.redundant_call_rate == pytest.approx(1 / 9)
    assert rep.low_confidence_rate == pytest.approx(1 / 8)  # only the routing Choice at 0.31
    assert rep.models == {"mock-fixture": 7, "mock-1.0": 1}
    assert rep.provider_latency_ms["p95"] is not None


async def test_report_never_claims_accuracy_without_labels(run_file: Path) -> None:
    rep = build_report(load_events(run_file).events)
    assert rep.labelled == 0 and rep.action_agreement is None and rep.calibration == []
    assert any("accuracy is not reported" in w for w in rep.warnings)


async def test_labels_add_agreement_and_calibration_with_a_small_sample_warning(
    run_file: Path,
) -> None:
    events = load_events(run_file).events
    for rec in events[:3]:
        append_label(run_file, rec.event.event_id, expected_action=rec.policy_result.action)
    append_label(run_file, events[3].event.event_id, expected_action="allow", note="wrong call")
    labelled = apply_labels(events, load_labels(labels_path(run_file)))
    rep = build_report(labelled)
    assert rep.labelled == 4 and rep.action_agreement == pytest.approx(3 / 4)
    assert rep.calibration and sum(row["n"] for row in rep.calibration) <= 4
    assert any("only 4 labelled row" in w for w in rep.warnings)
    assert load_events(run_file).events[0].outcome is None, "the event log itself is never edited"


def test_a_label_needs_something_to_say(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="needs"):
        append_label(tmp_path / "x.jsonl", "e1")


async def test_report_warns_about_mixed_model_versions_and_changed_questions(
    run_file: Path,
) -> None:
    events = load_events(run_file).events
    assert any("more than one resolved model" in w for w in build_report(events).warnings)
    tampered = [
        e.model_copy(update={"question_pack_fingerprint": "different"}) if i == 0 else e
        for i, e in enumerate(events)
    ]
    assert any("different question fingerprints" in w for w in build_report(tampered).warnings)


async def test_report_includes_policy_changes_when_a_replay_is_given(run_file: Path) -> None:
    events = load_events(run_file).events
    replay = replay_events(events, ToolLoopPolicy.for_replay("strict"))
    rep = build_report(events, replay=replay)
    assert rep.policy_changes is not None
    assert rep.policy_changes["changed"] == 1 and rep.policy_changes["transitions"] == {
        "allow->review": 1
    }


def test_an_empty_recording_reports_that_plainly() -> None:
    rep = build_report([])
    assert rep.total == 0 and rep.warnings == ["no events to report on"]

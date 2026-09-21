"""The command line: examples, replay, report, packs, and every clear-error case."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from jev_reactor import __version__, cli
from jev_reactor.cli import app
from jev_reactor.models import DecisionEvent

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
REPLAY = EX / "replay.jsonl"
runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Each test starts with no key, no mock flag, in a scratch directory."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_REACTOR_MOCK", raising=False)
    monkeypatch.chdir(tmp_path)
    argv, path = list(sys.argv), list(sys.path)
    yield
    sys.argv[:] = argv
    sys.path[:] = path
    import os

    os.environ.pop("JEV_REACTOR_MOCK", None)


def out(result: Result) -> str:
    text = result.output + (result.stderr if result.stderr_bytes else "")
    return re.sub(r"\s+", " ", text)  # tables wrap; compare on flattened text


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


@pytest.fixture
def run_copy(tmp_path: Path) -> Path:
    target = tmp_path / "runs.jsonl"
    shutil.copyfile(REPLAY, target)
    return target


# ---------------------------------------------------------------------------- basics


def test_help_lists_every_command() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for command in (
        "init",
        "run",
        "replay",
        "report",
        "validate-pack",
        "inspect",
        "label",
        "bench",
    ):
        assert command in result.output


def test_version() -> None:
    assert __version__ in invoke("--version").output


def test_no_core_module_can_phone_home() -> None:
    """No telemetry: only the TypeSafe adapter (and the SDK it wraps) may use the network."""
    forbidden = re.compile(
        r"^\s*(import|from)\s+(httpx2?|requests|urllib\.request|http\.client|socket|aiohttp|websockets)\b",
        re.M,
    )
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "src" / "jev_reactor").rglob("*.py")
        if p.name != "typesafe.py" and forbidden.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# ---------------------------------------------------------------------------- examples


def test_basic_decision_example_prints_the_readme_output() -> None:
    result = invoke("run", str(EX / "basic_decision.py"), "--mock")
    assert result.exit_code == 0, out(result)
    text = out(result)
    for line in (
        "Decision: skip",
        "Reason: redundant_tool_call",
        "Redundancy probability: 0.91",
        "Choice confidence: 0.86",
    ):
        assert line in text


def test_the_tool_loop_example_executes_only_allowed_calls(tmp_path: Path) -> None:
    result = invoke("run", str(EX / "tool_loop.py"), "--mock")
    assert result.exit_code == 0, out(result)
    text = out(result)
    assert "Executed by the demo tool server: ['search_invoices']" in text
    assert "Emails actually sent: 0" in text
    log = tmp_path / "decisions" / "tool_loop.jsonl"
    assert len(log.read_text(encoding="utf-8").splitlines()) == 8


def test_the_mcp_example_never_runs_a_blocked_call() -> None:
    result = invoke("run", str(EX / "mcp_proxy.py"), "--mock")
    assert result.exit_code == 0, out(result)
    assert "Ran on the demo server: ['search_invoices', 'send_email']" in out(result)
    assert "No blocked or unapproved call was executed." in out(result)


def test_the_mcp_example_in_observe_mode_still_honours_hard_rules() -> None:
    result = invoke("run", str(EX / "mcp_proxy.py"), "--mock", "--mode", "observe")
    assert result.exit_code == 0, out(result)
    assert "No blocked or unapproved call was executed." in out(result)


def test_the_context_example_builds_a_plan() -> None:
    result = invoke("run", str(EX / "context_compaction.py"), "--mock")
    assert result.exit_code == 0, out(result)
    assert "retain 6 compact 1 discard 1" in out(result)
    assert "Pinned in code, never sent to Jev: ['m4', 'm6', 'm8']" in out(result)


def test_running_live_without_a_key_is_a_clear_error() -> None:
    result = invoke("run", str(EX / "basic_decision.py"))
    assert result.exit_code == 2
    assert "TYPESAFE_API_KEY is not set" in out(result)


def test_running_a_missing_script_says_how_to_get_the_examples() -> None:
    result = invoke("run", "nope.py")
    assert result.exit_code == 2 and "no such file" in out(result) and "init" in out(result)


def test_init_then_run_works_from_the_scaffold(tmp_path: Path) -> None:
    """The README's first-screen flow, offline."""
    project = tmp_path / "proj"
    result = invoke("init", str(project))
    assert result.exit_code == 0, out(result)
    for expected in (
        "examples/tool_loop.py",
        "fixtures/safe_tool_call.json",
        "packs/tool-loop.yaml",
        ".env.example",
        "decisions",
    ):
        assert (project / expected).exists(), expected
    assert "TYPESAFE_API_KEY=" in (project / ".env.example").read_text(encoding="utf-8")

    (project / "examples" / "tool_loop.py").write_text("# edited", encoding="utf-8")
    again = invoke("init", str(project))
    assert (
        "left" in again.output and (project / "examples" / "tool_loop.py").read_text() == "# edited"
    )
    invoke("init", str(project), "--force")
    assert "edited" not in (project / "examples" / "tool_loop.py").read_text(encoding="utf-8")

    import os

    os.chdir(project)
    ran = invoke("run", "examples/tool_loop.py", "--mock")
    assert ran.exit_code == 0, out(ran)
    assert "Executed by the demo tool server: ['search_invoices']" in out(ran)


# ---------------------------------------------------------------------------- packs


@pytest.mark.parametrize("name", ["tool-loop", "context-retention", "agent-loop"])
def test_builtin_packs_validate(name: str) -> None:
    result = invoke("validate-pack", str(ROOT / "packs" / f"{name}.yaml"))
    assert result.exit_code == 0 and "OK" in result.output


def write_pack(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pack.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_validate_pack_fails_a_choice_without_an_abstain_option(tmp_path: Path) -> None:
    path = write_pack(
        tmp_path,
        "id: p\nquestions:\n  q:\n    type: choice\n    instructions: Which one?\n"
        "    criteria: {a: first, b: second}\n",
    )
    result = invoke("validate-pack", str(path))
    assert result.exit_code == 1
    assert "choice-missing-abstain" in out(result) and "FAILED" in out(result)


def test_validate_pack_warnings_only_fail_under_strict(tmp_path: Path) -> None:
    path = write_pack(
        tmp_path, "id: p\nquestions:\n  q:\n    type: noul\n    instructions: Is {tool} ok?\n"
    )
    assert invoke("validate-pack", str(path)).exit_code == 0
    strict = invoke("validate-pack", str(path), "--strict")
    assert strict.exit_code == 1 and "instruction-placeholder" in out(strict)


def test_validate_pack_names_an_invalid_question_type(tmp_path: Path) -> None:
    path = write_pack(tmp_path, "id: p\nquestions:\n  q:\n    type: rank\n    instructions: hi\n")
    result = invoke("validate-pack", str(path))
    assert result.exit_code == 2 and "questions.q" in out(result) and "type" in out(result)


def test_validate_pack_reports_missing_and_broken_files(tmp_path: Path) -> None:
    assert "cannot read" in out(invoke("validate-pack", str(tmp_path / "nope.yaml")))
    assert "not valid YAML" in out(invoke("validate-pack", str(write_pack(tmp_path, "id: [oops"))))


def test_export_pack_prints_and_writes(tmp_path: Path) -> None:
    printed = invoke("export-pack", "tool-loop")
    assert printed.exit_code == 0 and printed.output.startswith("# Generated by")
    assert (tmp_path / "x").exists() is False
    invoke("export-pack", "agent-loop", "--out", str(tmp_path / "x"))
    assert (tmp_path / "x" / "agent-loop.yaml").is_file()
    assert "unknown built-in pack" in out(invoke("export-pack", "bogus"))
    assert invoke("export-pack", "all").exit_code == 2


# ---------------------------------------------------------------------------- replay


def test_replay_dry_run_changes_nothing_on_disk(run_copy: Path) -> None:
    result = invoke("replay", str(run_copy), "--dry-run")
    assert result.exit_code == 0 and "0 decision(s) changed" in out(result)
    assert [p.name for p in run_copy.parent.iterdir()] == ["runs.jsonl"]


def test_replay_with_the_strict_policy_shows_what_changes(run_copy: Path) -> None:
    result = invoke("replay", str(run_copy), "--policy", "strict", "--dry-run")
    assert result.exit_code == 0
    assert "1 decision(s) changed" in out(result) and "allow->review: 1" in out(result)
    assert "evt_demo_09" in out(result)


def test_replay_writes_a_new_file_and_never_touches_the_original(run_copy: Path) -> None:
    before = run_copy.read_bytes()
    result = invoke("replay", str(run_copy), "--policy", "strict")
    assert result.exit_code == 0, out(result)
    assert run_copy.read_bytes() == before
    written = run_copy.with_name("runs.replay.strict.jsonl")
    events = [DecisionEvent.from_jsonl(line) for line in written.read_text().splitlines()]
    assert len(events) == 9
    assert events[8].policy_result.action == "review"
    assert events[8].policy_id == "tool-loop/strict+replay"


def test_replay_never_accepts_a_live_provider(run_copy: Path) -> None:
    result = invoke("replay", str(run_copy), "--provider", "typesafe")
    assert result.exit_code == 2 and "never calls a provider" in out(result)
    assert invoke("replay", str(run_copy), "--provider", "mock", "--dry-run").exit_code == 0


def test_replay_rejects_an_unknown_policy_preset(run_copy: Path) -> None:
    result = invoke("replay", str(run_copy), "--policy", "bogus", "--dry-run")
    assert result.exit_code == 2 and "unknown policy preset" in out(result)


def test_a_malformed_replay_line_is_reported_with_its_line_number(run_copy: Path) -> None:
    lines = run_copy.read_text(encoding="utf-8").splitlines()
    lines.insert(1, '{"this": "is not a decision event"}')
    run_copy.write_text("\n".join(lines) + "\n", encoding="utf-8")
    lenient = invoke("replay", str(run_copy), "--dry-run")
    assert lenient.exit_code == 0 and f"{run_copy}:2:" in out(lenient)
    strict = invoke("replay", str(run_copy), "--dry-run", "--strict")
    assert strict.exit_code == 2 and "line 2" in out(strict)


def test_a_truncated_last_line_is_a_warning_not_a_failure(run_copy: Path) -> None:
    text = run_copy.read_text(encoding="utf-8")
    run_copy.write_text(text + text.splitlines()[0][:80], encoding="utf-8")
    result = invoke("replay", str(run_copy), "--dry-run")
    assert result.exit_code == 0 and "incomplete" in out(result)


def test_a_recorded_choice_outside_its_criteria_is_called_out(run_copy: Path) -> None:
    rows = [json.loads(line) for line in run_copy.read_text(encoding="utf-8").splitlines()]
    answer = rows[0]["response"]["answers"]["next_action"]
    answer["choice"] = "teleport"
    answer["probabilities"] = {"teleport": 1.0}
    run_copy.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    result = invoke("replay", str(run_copy), "--dry-run")
    assert "recorded answer does not match the pack" in out(result)
    assert "'teleport' is not a declared option" in out(result)


def test_a_policy_returning_an_unknown_action_fails_the_replay(
    run_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Forgetful:
        policy_id = "forgetful"

        def decide(self, *, event: Any, response: Any) -> Any:
            return {"action": "call_tool", "reason_codes": ["x"]}

    monkeypatch.setattr(cli, "_resolve_policy", lambda *_a, **_k: Forgetful())
    result = invoke("replay", str(run_copy), "--dry-run")
    assert result.exit_code == 1
    assert "policy error" in out(result) and "invalid decision" in out(result)


def test_replaying_a_file_that_mixes_packs_needs_a_choice(run_copy: Path) -> None:
    from jev_reactor import ContextPolicy, JsonlSink, MockProvider, Reactor, context_pack

    async def add_context_event() -> None:
        sink = JsonlSink(run_copy)
        reactor = Reactor(
            MockProvider(),
            pack=context_pack(),
            policy=ContextPolicy(),
            sinks=[sink],
        )
        await reactor.decide(reactor.new_event("context_item", {"item": {"id": "x", "text": "hi"}}))
        sink.close()

    asyncio.run(add_context_event())
    mixed = invoke("replay", str(run_copy), "--dry-run")
    assert mixed.exit_code == 2 and "mixes packs" in out(mixed)
    chosen = invoke("replay", str(run_copy), "--dry-run", "--pack", "tool-loop")
    assert chosen.exit_code == 0 and "Replayed 9 event(s)" in out(chosen)


def test_replay_can_reapply_hard_rules_from_a_tool_inventory(
    run_copy: Path, tmp_path: Path
) -> None:
    tools = tmp_path / "tools.yaml"
    tools.write_text(
        "search_invoices: {risk: read, idempotent: true, requires_permission: 'invoices:read'}\n"
        "get_invoice_status: read\n"
        "send_email: {risk: irreversible, requires_permission: 'email:send'}\n",
        encoding="utf-8",
    )
    result = invoke("replay", str(run_copy), "--tools", str(tools), "--dry-run")
    assert result.exit_code == 0, out(result)
    assert "0 decision(s) changed" in out(result)


# ---------------------------------------------------------------------------- report


def test_report_states_only_what_the_recording_supports(run_copy: Path) -> None:
    result = invoke("report", str(run_copy))
    text = out(result)
    assert result.exit_code == 0
    assert "9 decided event(s)" in text and "Questions per request 7.0" in text
    assert "accuracy is not reported" in text
    assert "not_called 1" in text and "ok 8" in text


def test_report_json_is_machine_readable(run_copy: Path) -> None:
    result = invoke("report", str(run_copy), "--json")
    data = json.loads(result.output)
    assert data["total"] == 9 and data["labelled"] == 0 and data["action_agreement"] is None


def test_report_can_include_the_effect_of_another_policy(run_copy: Path) -> None:
    result = invoke("report", str(run_copy), "--policy", "strict")
    assert "Policy changes" in out(result) and "1 of 9" in out(result)


def test_labels_unlock_agreement_and_never_edit_the_run(run_copy: Path) -> None:
    before = run_copy.read_bytes()
    first = invoke("label", str(run_copy), "evt_demo_01", "--expected", "allow")
    assert first.exit_code == 0 and "labels.jsonl" in out(first)
    invoke("label", str(run_copy), "evt_demo_02", "--label", "incorrect", "--note", "should call")
    assert run_copy.read_bytes() == before
    text = out(invoke("report", str(run_copy)))
    assert "Labelled events 2" in text and "only 2 labelled row" in text


def test_label_rejects_unknown_events_and_bad_labels(run_copy: Path) -> None:
    assert "no event 'evt_nope'" in out(
        invoke("label", str(run_copy), "evt_nope", "--label", "correct")
    )
    assert invoke("label", str(run_copy), "evt_demo_01", "--label", "maybe").exit_code == 2
    assert invoke("label", str(run_copy), "evt_demo_01").exit_code == 2


# ---------------------------------------------------------------------------- inspect / bench


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (ROOT / "tests" / "fixtures" / "safe_tool_call.json", "scenario fixture"),
        (ROOT / "tests" / "fixtures" / "context_messages.json", "context scenario fixture"),
        (ROOT / "packs" / "tool-loop.yaml", "question pack"),
        (REPLAY, "a run of 9 decision event(s)"),
    ],
)
def test_inspect_recognises_what_it_is_shown(path: Path, expected: str) -> None:
    result = invoke("inspect", str(path))
    assert result.exit_code == 0 and expected in out(result)


def test_inspect_recognises_a_single_decision_event(tmp_path: Path) -> None:
    line = REPLAY.read_text(encoding="utf-8").splitlines()[0]
    single = tmp_path / "one.json"
    single.write_text(line, encoding="utf-8")
    assert "decision event" in out(invoke("inspect", str(single)))


def test_inspect_reports_bad_input_clearly(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    assert "not valid JSON/YAML" in out(invoke("inspect", str(broken)))
    assert "no such file" in out(invoke("inspect", str(tmp_path / "missing.json")))


def test_bench_measures_locally_and_makes_no_latency_claim() -> None:
    result = invoke("bench", "--n", "20")
    text = out(result)
    assert result.exit_code == 0, text
    assert "policy.decide()" in text and "Engineering target" in text
    assert "No latency claim is made until you do" in text


def test_bench_live_needs_a_key() -> None:
    result = invoke("bench", "--n", "5", "--live")
    assert result.exit_code == 2 and "TYPESAFE_API_KEY is not set" in out(result)

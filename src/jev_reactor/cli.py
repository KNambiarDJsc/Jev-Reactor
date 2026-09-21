"""Command line interface. No telemetry, no phone-home, no automatic uploads.

jev-reactor init                      scaffold examples, fixtures and packs here
jev-reactor run <file.py> [--mock]    run an example script
jev-reactor replay <runs.jsonl>       recompute decisions with another policy (no Jev)
jev-reactor report <runs.jsonl>       summarise a recorded run
jev-reactor validate-pack <file>      lint a question pack
jev-reactor inspect <file>            pretty-print a fixture, event, pack or run
jev-reactor label / export-pack / bench
"""

from __future__ import annotations

import json
import os
import runpy
import shutil
import sys
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from jev_reactor import __version__
from jev_reactor.config import ReactorConfig
from jev_reactor.demo import DEMO_TOOLS, data_dir, demo_loop, load_scenarios
from jev_reactor.errors import ConfigError, InvalidAnswerError, ReactorError, ReplayError
from jev_reactor.metrics import Report, build_report, percentile
from jev_reactor.models import DecisionEvent
from jev_reactor.packs import (
    BUILTIN_PACKS,
    PRESETS,
    AgentLoopPolicy,
    ContextPolicy,
    ToolLoopPolicy,
    ToolSpec,
)
from jev_reactor.packs.export import export_all, export_yaml
from jev_reactor.policy import Policy
from jev_reactor.providers.mock import MockProvider
from jev_reactor.questions import lint_examples, lint_pack, load_pack, validate_response
from jev_reactor.reactor import Reactor
from jev_reactor.replay import (
    append_label,
    apply_labels,
    labels_path,
    load_events,
    load_labels,
    replay_events,
)

app = typer.Typer(
    name="jev-reactor",
    help="Real-time typed decisions: Jev judges, ordinary code decides, adapters execute.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
console = Console()
err_console = Console(stderr=True, soft_wrap=True)  # never wrap a path mid-word

F = TypeVar("F", bound=Callable[..., Any])

ENV_EXAMPLE = """\
# Copy to .env (never commit .env). Only needed for live Jev calls;
# every example runs with --mock and no credentials.
TYPESAFE_API_KEY=
TYPESAFE_DEFAULT_MODEL=jev-latest
TYPESAFE_BASE_URL=https://api.typesafe.ai
JEV_TIMEOUT_SECONDS=1.0
"""


def fail(message: str, *, hint: str | None = None, code: int = 2) -> NoReturn:
    err_console.print(f"[bold red]Error:[/] {message}", highlight=False)
    if hint:
        err_console.print(f"[dim]{hint}[/]", highlight=False)
    raise typer.Exit(code)


def guarded(fn: F) -> F:
    """Turn library errors into one clear line and a non-zero exit code."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except ConfigError as exc:
            fail(str(exc), hint="See .env.example for the settings Jev Reactor reads.")
        except ReactorError as exc:
            fail(str(exc))

    return wrapper  # type: ignore[return-value]


def _print_version(value: bool) -> None:
    if value:
        console.print(f"jev-reactor {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,  # handled before Click checks that a command was given
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    # explicit path: dotenv's default search starts from *this package's* directory
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)


# ---------------------------------------------------------------------------- init


@app.command()
@guarded
def init(
    directory: Annotated[Path, typer.Argument(help="Where to scaffold (default: here).")] = Path(
        "."
    ),
    force: Annotated[bool, typer.Option(help="Overwrite existing files.")] = False,
) -> None:
    """Copy the examples, fixtures and packs here and write a .env.example."""
    root = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    plan: list[tuple[Path, Path]] = []
    for src_name, dst_name in (
        ("examples", "examples"),
        ("tests/fixtures", "fixtures"),
        ("packs", "packs"),
    ):
        src = root / src_name
        if not src.is_dir():
            src = root / Path(src_name).name  # packaged layout: _data/<name>
        if src.is_dir():
            plan += [
                (f, directory / dst_name / f.name) for f in sorted(src.iterdir()) if f.is_file()
            ]
    if not plan:
        fail(
            "could not find the packaged examples to copy",
            hint="Is the package installed correctly?",
        )
    written, skipped = 0, 0
    for src_file, dst_file in plan:
        if dst_file.exists() and not force:
            skipped += 1
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_file, dst_file)
        written += 1
    env = directory / ".env.example"
    if force or not env.exists():
        env.write_text(ENV_EXAMPLE, encoding="utf-8", newline="\n")
        written += 1
    (directory / "decisions").mkdir(exist_ok=True)
    console.print(f"Wrote {written} file(s), left {skipped} existing file(s) alone.")
    console.print("\nNext:")
    console.print("  jev-reactor run examples/tool_loop.py --mock     # offline, no credentials")
    console.print("  export TYPESAFE_API_KEY=...                        # then, live:")
    console.print("  jev-reactor run examples/tool_loop.py")
    console.print("\nDecision logs go to ./decisions/ (add it to .gitignore).")


# ---------------------------------------------------------------------------- run


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@guarded
def run(
    ctx: typer.Context,
    script: Annotated[Path, typer.Argument(help="A Python file that uses jev_reactor.")],
    mock: Annotated[
        bool, typer.Option("--mock", help="Use recorded answers: no key, no network.")
    ] = False,
) -> None:
    """Run an example script. Extra arguments are passed through to it."""
    if not script.is_file():
        fail(f"no such file: {script}", hint="Run `jev-reactor init` to copy the examples here.")
    if mock:
        os.environ["JEV_REACTOR_MOCK"] = "1"
    elif not os.environ.get("TYPESAFE_API_KEY") and os.environ.get("JEV_REACTOR_MOCK") != "1":
        err_console.print(
            "[yellow]Note:[/] TYPESAFE_API_KEY is not set. A script that calls Jev will stop "
            "with an error; add --mock for an offline run.",
            highlight=False,
        )
    argv = [str(script), *ctx.args]
    if mock and "--mock" not in argv:
        argv.append("--mock")
    sys.argv = argv
    sys.path.insert(0, str(script.resolve().parent))
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        raise typer.Exit(code) from None


# ---------------------------------------------------------------------------- pack tooling


def _example_bases(pack_file: Path) -> list[Path]:
    return [Path.cwd(), pack_file.parent, pack_file.parent.parent]


@app.command("validate-pack")
@guarded
def validate_pack(
    file: Annotated[Path, typer.Argument(help="A question pack (YAML).")],
    strict: Annotated[bool, typer.Option(help="Treat warnings as failures.")] = False,
) -> None:
    """Check a pack: schema, question shapes, and the lint rules (see docs/question-packs.md)."""
    pack = load_pack(file)
    issues = lint_pack(pack)
    issues += lint_examples(pack, _example_bases(file))
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    for issue in issues:
        color = "red" if issue.severity == "error" else "yellow"
        console.print(f"[{color}]{issue}[/]", highlight=False)
    label = (
        f"pack {pack.id!r} v{pack.version}: {len(pack.questions)} question(s), "
        f"fingerprint {pack.fingerprint()}"
    )
    if errors or (strict and warnings):
        console.print(
            f"[bold red]FAILED[/] {label} - {len(errors)} error(s), {len(warnings)} warning(s)"
        )
        raise typer.Exit(1)
    console.print(f"[bold green]OK[/] {label} - {len(warnings)} warning(s)")


@app.command("export-pack")
@guarded
def export_pack(
    name: Annotated[str, typer.Argument(help=f"One of: {', '.join(BUILTIN_PACKS)}, or 'all'.")],
    out: Annotated[
        Path | None, typer.Option(help="Write into this directory instead of stdout.")
    ] = None,
) -> None:
    """Print (or write) a built-in pack as YAML: a starting point for your own."""
    if name == "all":
        if out is None:
            fail("--out is required with 'all'")
        for path in export_all(out):
            console.print(f"wrote {path}")
        return
    text = export_yaml(name)
    if out is None:
        sys.stdout.write(text)
    else:
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{name}.yaml"
        target.write_text(text, encoding="utf-8", newline="\n")
        console.print(f"wrote {target}")


# ---------------------------------------------------------------------------- replay / report


def _load_tools(path: Path) -> list[ToolSpec]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read tool inventory {path}: {exc}") from exc
    if isinstance(data, dict):
        data = [{"name": k, **(v if isinstance(v, dict) else {"risk": v})} for k, v in data.items()]
    if not isinstance(data, list):
        raise ConfigError(f"{path} must contain a list of tools or a mapping of name -> spec")
    try:
        return [ToolSpec.model_validate(item) for item in data]
    except ValueError as exc:
        raise ConfigError(f"{path}: invalid tool entry: {exc}") from exc


def _resolve_policy(pack_id: str, name: str, tools_file: Path | None) -> Policy:
    if pack_id == "tool-loop":
        if name not in PRESETS:
            raise ReactorError(f"unknown policy preset {name!r}; choose from {sorted(PRESETS)}")
        if tools_file is not None:
            return ToolLoopPolicy.from_preset(name, _load_tools(tools_file))
        return ToolLoopPolicy.for_replay(name)
    if name != "default":
        raise ReactorError(f"pack {pack_id!r} has only the 'default' policy, not {name!r}")
    if pack_id == "context-retention":
        return ContextPolicy()
    if pack_id == "agent-loop":
        return AgentLoopPolicy()
    raise ReactorError(
        f"no built-in policy for pack {pack_id!r}; replay it from Python with your own policy"
    )


def _select_pack(events: list[DecisionEvent], pack: str | None) -> tuple[str, list[DecisionEvent]]:
    ids = sorted({e.question_pack_id for e in events})
    if not ids:
        raise ReplayError("the file contains no events")
    if pack is None:
        if len(ids) > 1:
            raise ReplayError(f"the file mixes packs ({', '.join(ids)}); choose one with --pack")
        pack = ids[0]
    chosen = [e for e in events if e.question_pack_id == pack]
    if not chosen:
        raise ReplayError(f"no events for pack {pack!r}; the file has: {', '.join(ids)}")
    return pack, chosen


def _read_run(file: Path, strict: bool) -> list[DecisionEvent]:
    loaded = load_events(file, strict=strict)
    for issue in loaded.issues:
        style = "yellow" if issue.kind == "truncated_tail" else "red"
        err_console.print(f"[{style}]{file}:{issue.line}: {issue.message}[/]", highlight=False)
    return apply_labels(loaded.events, load_labels(labels_path(file)))


def _check_answers(pack_id: str, events: list[DecisionEvent]) -> list[str]:
    """Re-validate recorded answers against the built-in pack: catches out-of-criteria Choices."""
    factory = BUILTIN_PACKS.get(pack_id)
    if factory is None:
        return []
    questions = factory().questions
    problems = []
    for rec in events:
        if rec.response is not None and rec.provider_status == "ok":
            try:
                validate_response(rec.response, questions)
            except InvalidAnswerError as exc:
                problems.append(f"{rec.event.event_id}: {exc}")
    return problems


@app.command()
@guarded
def replay(
    file: Annotated[Path, typer.Argument(help="A JSONL run recorded by the Reactor.")],
    provider: Annotated[
        str, typer.Option(help="'recorded' or 'mock'. Replay never calls Jev.")
    ] = "recorded",
    policy: Annotated[str, typer.Option(help="Policy preset: default or strict.")] = "default",
    tools: Annotated[
        Path | None, typer.Option(help="Tool inventory (YAML/JSON) to re-apply hard rules.")
    ] = None,
    pack: Annotated[str | None, typer.Option(help="Pack id, if the file mixes packs.")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the comparison; write nothing.")
    ] = False,
    out: Annotated[Path | None, typer.Option(help="Where to write the replayed run.")] = None,
    strict: Annotated[bool, typer.Option(help="Fail on any malformed line.")] = False,
    show_all: Annotated[bool, typer.Option("--all", help="List every changed decision.")] = False,
) -> None:
    """Recompute policy over the recorded answers. Never calls Jev, never uses the network."""
    if provider not in {"recorded", "mock"}:
        fail(
            f"replay does not support --provider {provider!r}",
            hint="Replay recomputes policy over recorded responses and never calls a provider.",
        )
    events = _read_run(file, strict)
    pack_id, chosen = _select_pack(events, pack)
    pol = _resolve_policy(pack_id, policy, tools)
    for problem in _check_answers(pack_id, chosen):
        err_console.print(
            f"[yellow]recorded answer does not match the pack:[/] {problem}", highlight=False
        )
    result = replay_events(chosen, pol, config=ReactorConfig())

    console.print(
        f"Replayed [bold]{len(result.rows)}[/] event(s) of pack {pack_id!r} with policy "
        f"{getattr(pol, 'policy_id', policy)!r}; [bold]{len(result.changed)}[/] decision(s) "
        "changed."
    )
    if result.skipped_late:
        console.print(f"[dim]{result.skipped_late} late record(s) excluded.[/]")
    if result.transitions():
        console.print("  " + "  ".join(f"{k}: {v}" for k, v in result.transitions().items()))
    if result.changed:
        table = Table()
        for column in ("seq", "event", "tool", "recorded", "replayed"):
            table.add_column(column, overflow="fold")
        for row in result.changed if show_all else result.changed[:25]:
            call = row.record.event.proposed_action or {}
            table.add_row(
                str(row.record.event.sequence),
                row.record.event.event_id,
                str(call.get("tool", "-")),
                f"{row.original.action} ({', '.join(row.original.reason_codes)})",
                f"{row.replayed.action} ({', '.join(row.replayed.reason_codes)})",
            )
        console.print(table)
        if not show_all and len(result.changed) > 25:
            console.print(f"[dim]... and {len(result.changed) - 25} more (use --all)[/]")
    broken = [r for r in result.rows if r.replayed.reason_codes == ["policy_error"]]
    for row in broken[:5]:
        err_console.print(f"[red]policy error:[/] {row.note}", highlight=False)

    if not dry_run:
        target = out or file.with_name(f"{file.stem}.replay.{policy}.jsonl")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            for row in result.rows:
                rec = row.record.model_copy(
                    update={
                        "policy_result": row.replayed,
                        "policy_id": getattr(pol, "policy_id", policy),
                    }
                )
                fh.write(rec.to_jsonl() + "\n")
        console.print(f"Wrote {target}")
    if broken:
        raise typer.Exit(1)


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fmt_ms(stats: dict[str, float | None]) -> str:
    if not stats or not stats.get("n"):
        return "n/a"
    mean, p95 = stats.get("mean"), stats.get("p95")
    return f"mean {mean:.1f} ms, p95 {p95:.1f} ms (n={int(stats['n'] or 0)})"


def _print_report(rep: Report, title: str) -> None:
    console.print(f"[bold]{title}[/]  ({rep.total} decided event(s))")
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_row(
        "Provider status", "  ".join(f"{k} {v}" for k, v in sorted(rep.provider_status.items()))
    )
    table.add_row("Actions", "  ".join(f"{k} {v}" for k, v in sorted(rep.actions.items())))
    table.add_row("Provider latency", _fmt_ms(rep.provider_latency_ms))
    table.add_row("Decision latency", _fmt_ms(rep.decision_latency_ms))
    table.add_row(
        "Questions per request",
        "n/a" if rep.mean_question_count is None else f"{rep.mean_question_count:.1f}",
    )
    table.add_row("Low-confidence rate", _fmt_pct(rep.low_confidence_rate))
    table.add_row("Redundant-call rate", _fmt_pct(rep.redundant_call_rate))
    table.add_row("Context compaction rate", _fmt_pct(rep.context_compaction_rate))
    table.add_row(
        "Resolved models", "  ".join(f"{k} x{v}" for k, v in sorted(rep.models.items())) or "n/a"
    )
    table.add_row("Input tokens", str(rep.total_input_tokens))
    if rep.labelled:
        table.add_row(
            "Labelled events", f"{rep.labelled} (action agreement {_fmt_pct(rep.action_agreement)})"
        )
    else:
        table.add_row("Labelled events", "0 (accuracy is not reported)")
    if rep.policy_changes:
        pc = rep.policy_changes
        moves = ", ".join(f"{k}: {v}" for k, v in pc["transitions"].items()) or "none"
        table.add_row("Policy changes", f"{pc['changed']} of {pc['replayed']} ({moves})")
    console.print(table)
    if rep.calibration:
        cal = Table(
            "confidence",
            "n",
            "mean confidence",
            "agreement",
            title="Calibration (labelled events only)",
        )
        for row in rep.calibration:
            cal.add_row(
                row["confidence_range"],
                str(row["n"]),
                str(row["mean_confidence"]),
                str(row["agreement"]),
            )
        console.print(cal)
    for warning in rep.warnings:
        console.print(f"[yellow]note:[/] {warning}", highlight=False)


@app.command()
@guarded
def report(
    file: Annotated[Path, typer.Argument(help="A JSONL run recorded by the Reactor.")],
    policy: Annotated[
        str | None, typer.Option(help="Also show what this policy preset would change.")
    ] = None,
    tools: Annotated[Path | None, typer.Option(help="Tool inventory for --policy.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    strict: Annotated[bool, typer.Option(help="Fail on any malformed line.")] = False,
) -> None:
    """Summarise a recorded run. Reports accuracy only for events you have labelled."""
    events = _read_run(file, strict)
    replayed = None
    if policy:
        pack_id, chosen = _select_pack(events, None)
        replayed = replay_events(chosen, _resolve_policy(pack_id, policy, tools))
    rep = build_report(events, replay=replayed)
    if as_json:
        sys.stdout.write(json.dumps(rep.to_dict(), indent=2, default=str) + "\n")
        return
    _print_report(rep, str(file))


@app.command()
@guarded
def label(
    file: Annotated[Path, typer.Argument(help="The JSONL run.")],
    event_id: Annotated[str, typer.Argument(help="Event id (see `jev-reactor inspect`).")],
    label: Annotated[str | None, typer.Option(help="correct or incorrect.")] = None,
    expected: Annotated[
        str | None, typer.Option(help="The action that should have been taken.")
    ] = None,
    note: Annotated[str | None, typer.Option(help="Free text.")] = None,
) -> None:
    """Label one decision. Written to a sidecar file; the run itself is never edited."""
    known = {e.event.event_id for e in load_events(file).events}
    if event_id not in known:
        fail(f"no event {event_id!r} in {file}")
    if label not in {None, "correct", "incorrect"}:
        fail("--label must be 'correct' or 'incorrect'")
    path = append_label(file, event_id, label=label, expected_action=expected, note=note)
    console.print(f"Recorded in {path}")


# ---------------------------------------------------------------------------- inspect


@app.command()
@guarded
def inspect(
    file: Annotated[Path, typer.Argument(help="A fixture, event, pack or JSONL run.")],
    limit: Annotated[int, typer.Option(help="Rows to show for a JSONL run.")] = 10,
) -> None:
    """Pretty-print a JSON/YAML/JSONL file and say what kind of file it is."""
    if not file.is_file():
        fail(f"no such file: {file}")
    if file.suffix == ".jsonl":
        loaded = load_events(file)
        console.print(f"[bold]{file}[/]: a run of {len(loaded.events)} decision event(s)")
        table = Table("seq", "event", "pack", "status", "action", "reasons", "conf")
        for rec in loaded.events[:limit]:
            d = rec.policy_result
            table.add_row(
                str(rec.event.sequence),
                rec.event.event_id,
                rec.question_pack_id,
                rec.provider_status,
                d.action,
                ", ".join(d.reason_codes),
                "-" if d.confidence is None else f"{d.confidence:.2f}",
            )
        console.print(table)
        for issue in loaded.issues:
            err_console.print(f"[yellow]line {issue.line}:[/] {issue.message}", highlight=False)
        return
    try:
        text = file.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if file.suffix in {".yaml", ".yml"} else json.loads(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        fail(f"{file} is not valid JSON/YAML: {exc}")
    kind = "data"
    if isinstance(data, dict):
        if "policy_result" in data:
            kind = "decision event"
        elif "questions" in data and "id" in data:
            kind = "question pack"
        elif "event" in data and ("response" in data or "answers" in data):
            kind = "scenario fixture (event + recorded answers)"
        elif "items" in data:
            kind = "context scenario fixture"
        elif "event_id" in data and "event_type" in data:
            kind = "reactor event"
    console.print(f"[bold]{file}[/]: {kind}")
    console.print_json(json.dumps(data, default=str))


# ---------------------------------------------------------------------------- bench


@app.command()
@guarded
def bench(
    n: Annotated[int, typer.Option(help="Iterations for the local benchmark.")] = 500,
    live: Annotated[
        bool, typer.Option("--live", help="Also time real requests (needs a key).")
    ] = False,
    live_n: Annotated[
        int, typer.Option(help="Requests for --live (kept small: rate limits apply).")
    ] = 20,
) -> None:
    """Measure, on this machine, what the README refuses to promise.

    The local numbers time the policy and the Reactor with a mock provider. --live times real
    requests through your network and your account's current load; it is a measurement of your
    environment, not a benchmark of Jev.
    """
    import asyncio

    scenario = next(s for s in load_scenarios(["safe_tool_call"]))
    loop = demo_loop()
    reactor = Reactor(MockProvider(scenario.answers), pack=loop.pack, policy=loop.policy)
    event = reactor.new_event(
        scenario.event["event_type"],
        scenario.event["state"],
        goal=scenario.event["goal"],
        proposed_action=scenario.event["proposed_action"],
        metadata=scenario.event["metadata"],
    )
    response = asyncio.run(
        MockProvider(scenario.answers).evaluate(
            state=reactor.preview_state(event), questions=loop.pack.questions, timeout=1.0
        )
    )

    policy_ms = []
    for _ in range(n):
        started = time.perf_counter()
        loop.policy.decide(event=event, response=response)
        policy_ms.append((time.perf_counter() - started) * 1000)

    async def timed_decisions() -> list[float]:
        out = []
        for _ in range(n):
            started = time.perf_counter()
            await reactor.decide(event)
            out.append((time.perf_counter() - started) * 1000)
        return out

    reactor_ms = asyncio.run(timed_decisions())
    table = Table(
        "measurement",
        "p50 ms",
        "p95 ms",
        "max ms",
        title=f"Local, n={n} (mock provider, no network)",
    )
    for name, values in (
        ("policy.decide()", policy_ms),
        ("Reactor.decide() end to end", reactor_ms),
    ):
        table.add_row(
            name,
            f"{percentile(values, 50):.3f}",
            f"{percentile(values, 95):.3f}",
            f"{max(values):.3f}",
        )
    console.print(table)
    p95 = percentile(policy_ms, 95) or 0.0
    verdict = "meets" if p95 < 5.0 else "MISSES"
    console.print(
        f"Engineering target: policy p95 under 5 ms. This machine {verdict} it ({p95:.3f} ms)."
    )

    if not live:
        console.print(
            "\n[dim]Add --live to time real requests. No latency claim is made until you do.[/]"
        )
        return
    if not os.environ.get("TYPESAFE_API_KEY"):
        fail(
            "TYPESAFE_API_KEY is not set",
            hint="--live needs a key; the local numbers above need none.",
        )
    from jev_reactor.providers.typesafe import TypeSafeProvider

    async def live_run() -> tuple[list[float], int]:
        latencies, failures = [], 0
        async with TypeSafeProvider() as provider:
            state = reactor.preview_state(event)
            for _ in range(live_n):
                started = time.perf_counter()
                try:
                    await provider.evaluate(
                        state=state, questions=loop.pack.questions, timeout=10.0
                    )
                    latencies.append((time.perf_counter() - started) * 1000)
                except ReactorError:
                    failures += 1
        return latencies, failures

    lat, failed = asyncio.run(live_run())
    console.print(
        f"\nLive, {live_n} sequential request(s) with all {len(loop.pack.questions)} questions: "
        f"{len(lat)} ok, {failed} failed."
    )
    if lat:
        console.print(
            f"  p50 {percentile(lat, 50):.0f} ms   p95 {percentile(lat, 95):.0f} ms   "
            f"max {max(lat):.0f} ms"
        )
    console.print(
        "[dim]This measures your network and your account's current load, nothing more.[/]"
    )


__all__ = ["DEMO_TOOLS", "app"]

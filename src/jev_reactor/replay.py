"""Replay recorded runs against a (possibly different) policy. Never calls a provider.

Replay recomputes the *policy* over the recorded responses. It cannot change what Jev
answered; it answers "what would this policy have decided on the same answers?", which is
how you compare threshold sets before turning one on.

Limits, by design:

* ``not_called`` events (hard rules, oversized state) are kept as recorded unless the policy
  can re-derive them from the persisted event. Rules that read ``state`` (for example exact
  duplicates) need ``persist_state="redacted"`` at record time.
* ``late`` records are second lines for an event that already has a decision. They are
  counted in reports but excluded from replay so no event is decided twice.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jev_reactor.config import ReactorConfig
from jev_reactor.errors import PolicyError, ReplayError
from jev_reactor.models import ActionDecision, DecisionEvent
from jev_reactor.policy import Policy, decision

LABELS_SUFFIX = ".labels.jsonl"


@dataclass
class LoadIssue:
    line: int
    message: str
    kind: str = "malformed"  # or "truncated_tail"


@dataclass
class LoadResult:
    events: list[DecisionEvent] = field(default_factory=list)
    issues: list[LoadIssue] = field(default_factory=list)


def _lines(path: Path) -> Iterator[tuple[int, str, bool]]:
    """(line number, text, is_last_line_without_newline)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReplayError(f"cannot read {path}: {exc.strerror or exc}") from exc
    raw = text.split("\n")
    ends_with_newline = text.endswith("\n")
    for i, line in enumerate(raw, start=1):
        if not line.strip():
            continue
        yield i, line, (i == len(raw) and not ends_with_newline)


def load_events(path: str | Path, *, strict: bool = False) -> LoadResult:
    """Read a JSONL file of DecisionEvents.

    A truncated final line (an interrupted write) is reported but never fatal. Any other
    malformed line raises ``ReplayError`` (with its line number) in strict mode and is
    collected in ``issues`` otherwise.
    """
    p = Path(path)
    result = LoadResult()
    for number, line, unterminated in _lines(p):
        try:
            result.events.append(DecisionEvent.from_jsonl(line))
        except (ValidationError, ValueError) as exc:
            detail = _brief(exc)
            if unterminated:
                result.issues.append(
                    LoadIssue(
                        number, "last line is incomplete (interrupted write?)", "truncated_tail"
                    )
                )
            elif strict:
                raise ReplayError(f"not a valid DecisionEvent: {detail}", line=number) from exc
            else:
                result.issues.append(LoadIssue(number, f"not a valid DecisionEvent: {detail}"))
    return result


def _brief(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        return f"{loc}: {first['msg']}"
    return str(exc)


def labels_path(events_path: str | Path) -> Path:
    p = Path(events_path)
    return p.with_name(p.name + LABELS_SUFFIX)


def load_labels(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read a labels sidecar (``<events>.labels.jsonl``). The last label per event wins."""
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for number, line, _ in _lines(p):
        try:
            data = json.loads(line)
            out[str(data["event_id"])] = {k: v for k, v in data.items() if k != "event_id"}
        except (ValueError, KeyError, TypeError) as exc:
            raise ReplayError("label line needs an event_id", line=number) from exc
    return out


def apply_labels(
    events: Iterable[DecisionEvent], labels: Mapping[str, dict[str, Any]]
) -> list[DecisionEvent]:
    """Merge sidecar labels into each event's ``outcome`` without touching the file."""
    merged = []
    for rec in events:
        label = labels.get(rec.event.event_id)
        if label:
            rec = rec.model_copy(update={"outcome": {**(rec.outcome or {}), **label}})
        merged.append(rec)
    return merged


def append_label(
    events_path: str | Path,
    event_id: str,
    *,
    label: str | None = None,
    expected_action: str | None = None,
    note: str | None = None,
) -> Path:
    """Append one label line to the sidecar (append-only, like the event log)."""
    if label is None and expected_action is None:
        raise ReplayError("a label needs --label and/or --expected")
    entry = {"event_id": event_id, "label": label, "expected_action": expected_action, "note": note}
    path = labels_path(events_path)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({k: v for k, v in entry.items() if v is not None}) + "\n")
    return path


# ---------------------------------------------------------------------------- replay


@dataclass
class ReplayRow:
    record: DecisionEvent
    original: ActionDecision
    replayed: ActionDecision
    #: why the replayed decision is not a fresh policy result, if it is not
    note: str | None = None

    @property
    def changed(self) -> bool:
        return (self.original.action, self.original.reason_codes) != (
            self.replayed.action,
            self.replayed.reason_codes,
        )

    @property
    def action_changed(self) -> bool:
        return self.original.action != self.replayed.action


@dataclass
class ReplayResult:
    rows: list[ReplayRow] = field(default_factory=list)
    skipped_late: int = 0

    @property
    def changed(self) -> list[ReplayRow]:
        return [r for r in self.rows if r.action_changed]

    def transitions(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.changed:
            key = f"{r.original.action}->{r.replayed.action}"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))


def replay_events(
    events: Iterable[DecisionEvent],
    policy: Policy,
    *,
    config: ReactorConfig | None = None,
    strict: bool = False,
) -> ReplayResult:
    """Recompute every decision with ``policy`` over the recorded responses.

    No provider is involved and none can be: the function takes none.
    """
    cfg = config or ReactorConfig()
    result = ReplayResult()
    for rec in events:
        if rec.provider_status == "late":
            result.skipped_late += 1
            continue
        original = rec.policy_result
        replayed, note = _replay_one(rec, policy, cfg, strict)
        replayed = replayed.model_copy(
            update={
                "event_id": original.event_id,
                "sequence": original.sequence,
                "provider_status": original.provider_status,
            }
        )
        result.rows.append(ReplayRow(rec, original, replayed, note))
    return result


def _replay_one(
    rec: DecisionEvent, policy: Policy, cfg: ReactorConfig, strict: bool
) -> tuple[ActionDecision, str | None]:
    status = rec.provider_status
    event = rec.event
    try:
        if status == "ok" and rec.response is not None:
            out = policy.decide(event=event, response=rec.response)
            if isinstance(out, Mapping):
                out = ActionDecision.model_validate(out)
            if not isinstance(out, ActionDecision):
                raise PolicyError(f"policy returned {type(out).__name__}, not an ActionDecision")
            return out, None
        if status == "not_called":
            hook = getattr(policy, "pre_check", None)
            fresh = hook(event) if hook else None
            if fresh is not None:
                return fresh, None
            return rec.policy_result, "kept as recorded: not re-derivable from the persisted event"
        if status in {"timeout", "error", "circuit_open"}:
            unavailable = getattr(policy, "on_unavailable", None)
            if unavailable is not None:
                return unavailable(event, status, cfg), None
            return rec.policy_result, "kept as recorded: policy has no failure hook"
        return rec.policy_result, f"kept as recorded: status {status}"
    except (ValidationError, PolicyError) as exc:
        message = (
            f"policy {getattr(policy, 'policy_id', type(policy).__name__)} returned an invalid "
            f"decision for event {event.event_id}: {_brief(exc)}"
        )
        if strict:
            raise PolicyError(message) from exc
        return decision("review", "policy_error", target="operator"), message
    except Exception as exc:
        if strict:
            raise
        return (
            decision("review", "policy_error", target="operator", error_type=type(exc).__name__),
            f"policy raised {type(exc).__name__}",
        )

"""Local reports over recorded runs.

The report states only what the recording supports. Accuracy is reported **only** for events
that carry a labelled outcome; otherwise it says so. Latency is measured, not promised.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jev_reactor.models import DecisionEvent, DecisionResponse
from jev_reactor.replay import ReplayResult

LOW_CONFIDENCE_FLOOR = 0.60
#: below this many labelled rows, thresholds fitted to labels should not be trusted
MIN_LABELS_FOR_CALIBRATION = 100
REDUNDANT_REASONS = {"redundant_tool_call", "exact_duplicate_call"}
CALIBRATION_BINS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.0001))


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..100). ``None`` for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass
class Report:
    total: int = 0
    provider_status: dict[str, int] = field(default_factory=dict)
    actions: dict[str, int] = field(default_factory=dict)
    provider_latency_ms: dict[str, float | None] = field(default_factory=dict)
    decision_latency_ms: dict[str, float | None] = field(default_factory=dict)
    mean_question_count: float | None = None
    low_confidence_rate: float | None = None
    redundant_call_rate: float | None = None
    context_compaction_rate: float | None = None
    models: dict[str, int] = field(default_factory=dict)
    pack_fingerprints: dict[str, list[str]] = field(default_factory=dict)
    late_records: int = 0
    total_input_tokens: int = 0
    labelled: int = 0
    action_agreement: float | None = None
    calibration: list[dict[str, Any]] = field(default_factory=list)
    policy_changes: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _low_confidence(rec: DecisionEvent) -> bool | None:
    """True if any Choice/Score answer is below the floor; None when there is none."""
    if rec.response is None:
        return None
    confs = [a.confidence for a in rec.response.answers.values() if a.confidence is not None]
    if not confs:
        return None
    return min(confs) < LOW_CONFIDENCE_FLOOR


def build_report(events: Iterable[DecisionEvent], *, replay: ReplayResult | None = None) -> Report:
    records = list(events)
    decided = [r for r in records if r.provider_status != "late"]
    rep = Report(total=len(decided), late_records=len(records) - len(decided))
    if not decided:
        rep.warnings.append("no events to report on")
        return rep

    rep.provider_status = dict(Counter(r.provider_status for r in decided))
    rep.actions = dict(Counter(r.policy_result.action for r in decided))

    responses: list[DecisionResponse] = [r.response for r in decided if r.response is not None]
    provider_ms = [x.latency_ms for x in responses]
    wall_ms = [r.decision_latency_ms for r in decided if r.decision_latency_ms is not None]
    rep.provider_latency_ms = {
        "n": float(len(provider_ms)),
        "mean": _mean(provider_ms),
        "p95": percentile(provider_ms, 95),
    }
    rep.decision_latency_ms = {
        "n": float(len(wall_ms)),
        "mean": _mean(wall_ms),
        "p95": percentile(wall_ms, 95),
    }

    with_response = [r for r in decided if r.response is not None]
    if responses:
        rep.mean_question_count = _mean([float(len(x.answers)) for x in responses])
    flags = [f for f in (_low_confidence(r) for r in with_response) if f is not None]
    if flags:
        rep.low_confidence_rate = sum(flags) / len(flags)

    tool_events = [r for r in decided if r.question_pack_id == "tool-loop"]
    if tool_events:
        hits = sum(1 for r in tool_events if REDUNDANT_REASONS & set(r.policy_result.reason_codes))
        rep.redundant_call_rate = hits / len(tool_events)
    ctx_events = [r for r in decided if r.question_pack_id == "context-retention"]
    if ctx_events:
        compacted = sum(1 for r in ctx_events if r.policy_result.action in {"compact", "skip"})
        rep.context_compaction_rate = compacted / len(ctx_events)

    rep.models = dict(Counter(x.model for x in responses))
    for r in decided:
        fps = rep.pack_fingerprints.setdefault(r.question_pack_id, [])
        if r.question_pack_fingerprint and r.question_pack_fingerprint not in fps:
            fps.append(r.question_pack_fingerprint)
    rep.total_input_tokens = sum(int(x.usage.get("input_tokens") or 0) for x in responses)

    _labelled(rep, decided)
    if replay is not None:
        rep.policy_changes = {
            "replayed": len(replay.rows),
            "changed": len(replay.changed),
            "transitions": replay.transitions(),
            "kept_as_recorded": sum(1 for r in replay.rows if r.note),
        }

    if len(rep.models) > 1:
        rep.warnings.append(
            "more than one resolved model version in this run "
            f"({', '.join(sorted(rep.models))}); thresholds tuned on one may not hold on the "
            "other. Pin TYPESAFE_DEFAULT_MODEL to a versioned id."
        )
    for pack_id, fps in rep.pack_fingerprints.items():
        if len(fps) > 1:
            rep.warnings.append(
                f"pack {pack_id!r} appears with {len(fps)} different question fingerprints; "
                "its wording changed during this run, so decisions are not directly comparable."
            )
    if rep.late_records:
        rep.warnings.append(
            f"{rep.late_records} late record(s): responses arrived after the deadline and were "
            "not applied. Consider a longer deadline if this is common."
        )
    return rep


def _labelled(rep: Report, decided: list[DecisionEvent]) -> None:
    labelled = [
        r
        for r in decided
        if r.outcome and (r.outcome.get("expected_action") or r.outcome.get("label"))
    ]
    rep.labelled = len(labelled)
    if not labelled:
        rep.warnings.append(
            "no labelled outcomes: accuracy is not reported. Add labels with "
            "`jev-reactor label` to measure agreement and calibrate thresholds."
        )
        return

    def correct(r: DecisionEvent) -> bool | None:
        out = r.outcome or {}
        if out.get("expected_action"):
            return bool(out["expected_action"] == r.policy_result.action)
        if out.get("label") in {"correct", "incorrect"}:
            return bool(out["label"] == "correct")
        return None

    verdicts = [(r, correct(r)) for r in labelled]
    scored = [(r, v) for r, v in verdicts if v is not None]
    if scored:
        rep.action_agreement = sum(1 for _, v in scored if v) / len(scored)
    for lo, hi in CALIBRATION_BINS:
        bucket = [(r, v) for r, v in scored if r.policy_result.confidence is not None
                  and lo <= r.policy_result.confidence < hi]  # fmt: skip
        if bucket:
            rep.calibration.append(
                {
                    "confidence_range": f"{lo:.1f}-{min(hi, 1.0):.1f}",
                    "n": len(bucket),
                    "mean_confidence": round(
                        _mean([r.policy_result.confidence or 0.0 for r, _ in bucket]) or 0.0, 3
                    ),
                    "agreement": round(sum(1 for _, v in bucket if v) / len(bucket), 3),
                }
            )
    if len(scored) < MIN_LABELS_FOR_CALIBRATION:
        rep.warnings.append(
            f"only {len(scored)} labelled row(s); fewer than about {MIN_LABELS_FOR_CALIBRATION} "
            "is too few to trust thresholds fitted to them."
        )

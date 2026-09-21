"""Core data models. Every public model is JSON-serialisable.

Design notes (each backed by the TypeSafe docs or community measurements, see
``docs/design-notes.md``):

* Noul answers carry **no** confidence; only Choice and Score answers do.
* Score ``probabilities`` / ``legend`` are keyed by integer level in the Python SDK and by
  string on the wire. We normalise to **string** keys so persisted JSONL is stable.
* Score levels are 0-indexed: a rubric of *n* levels yields a score in ``[0, n - 1]``.
* Out-of-range values are rejected, never clamped.
* Probabilities come back rounded to two decimals, so the sum check is rounding-aware.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

QuestionType = Literal["noul", "choice", "score"]
ActionName = Literal["allow", "skip", "compact", "route", "review", "block", "stop", "fallback"]
ProviderStatus = Literal["ok", "timeout", "error", "circuit_open", "late", "stale", "not_called"]
FailureMode = Literal["allow", "review", "block", "fallback", "skip"]

ACTIONS: tuple[str, ...] = (
    "allow",
    "skip",
    "compact",
    "route",
    "review",
    "block",
    "stop",
    "fallback",
)

MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10

# Wire probabilities are rounded to two decimals, so each entry can be off by up to 0.005.
_ROUNDING_STEP = 0.005
PROBABILITY_SUM_TOLERANCE_CAP = 0.05

#: option names that let a Choice abstain instead of guessing
ABSTAIN_NAMES = frozenset(
    {
        "other",
        "none",
        "none_of_the_above",
        "none of the above",
        "unclear",
        "unknown",
        "not_sure",
        "unsure",
        "n/a",
        "abstain",
        "escalate",
    }
)


def canonical_json(value: Any) -> str:
    """Stable JSON used for fingerprints and digests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def sha256_hex(value: Any, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def is_probability(value: float) -> bool:
    return math.isfinite(value) and 0.0 <= value <= 1.0


def probability_sum_tolerance(probabilities: dict[str, float]) -> float:
    """Rounding-aware tolerance for "values sum to approximately 1".

    The original brief suggested a flat 1e-3, which rejects legitimate two-decimal
    responses such as three options at 0.33 (sum 0.99).
    """
    nonzero = sum(1 for p in probabilities.values() if p > 0)
    return min(_ROUNDING_STEP * max(2, nonzero), PROBABILITY_SUM_TOLERANCE_CAP)


class ReactorEvent(BaseModel):
    """One thing that happened in the host application."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    timestamp: datetime
    event_type: str = Field(min_length=1)
    state: dict[str, Any]
    goal: str | None = None
    proposed_action: dict[str, Any] | None = None
    #: never put secrets here; redaction runs before persistence but is best-effort
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class QuestionSpec(BaseModel):
    """A single atomic question. Question ids are for your code; the model never sees them."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")
    type: QuestionType
    instructions: str | dict[str, Any] | list[Any]
    criteria: Any | None = None
    version: str = "1"
    tags: list[str] = Field(default_factory=list)

    @field_validator("instructions")
    @classmethod
    def _instructions_nonempty(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("instructions must not be empty")
        if isinstance(value, dict | list) and not value:
            raise ValueError("instructions must not be empty")
        return value

    @model_validator(mode="after")
    def _criteria_match_type(self) -> QuestionSpec:
        crit = self.criteria
        if self.type == "noul":
            if crit is not None and (
                not isinstance(crit, dict) or not set(crit) <= {"true", "false"}
            ):
                raise ValueError("noul criteria must be null or an object with true/false keys")
        elif self.type == "choice":
            if not isinstance(crit, dict) or not crit:
                raise ValueError("choice criteria must be an object mapping option -> description")
            if len(crit) < 2:
                raise ValueError("choice needs at least two options")
            if len(crit) > MAX_CHOICE_OPTIONS:
                raise ValueError(f"choice accepts at most {MAX_CHOICE_OPTIONS} options")
            if any(not isinstance(k, str) or not k for k in crit):
                raise ValueError("choice option names must be non-empty strings")
        else:  # score
            if not isinstance(crit, list):
                raise ValueError("score criteria must be an ordered list of level descriptions")
            if not MIN_SCORE_LEVELS <= len(crit) <= MAX_SCORE_LEVELS:
                raise ValueError(
                    f"score needs {MIN_SCORE_LEVELS}-{MAX_SCORE_LEVELS} levels, got {len(crit)}"
                )
        return self

    @property
    def options(self) -> list[str]:
        """Choice option names (empty for other types)."""
        if self.type == "choice" and isinstance(self.criteria, dict):
            return list(self.criteria)
        return []

    @property
    def level_count(self) -> int:
        """Number of Score levels (0 for other types). Levels are numbered from 0."""
        if self.type == "score" and isinstance(self.criteria, list):
            return len(self.criteria)
        return 0

    @property
    def abstain_options(self) -> list[str]:
        return [o for o in self.options if o.strip().lower() in ABSTAIN_NAMES]


class Answer(BaseModel):
    """A typed answer. ``noul`` / ``choice`` / ``score`` is set according to ``type``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str
    type: QuestionType
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = None
    legend: dict[str, Any] = Field(default_factory=dict)

    @field_validator("probabilities", "legend", mode="before")
    @classmethod
    def _stringify_keys(cls, value: Any) -> Any:
        # The SDK keys Score probabilities/legend by int; the wire uses str.
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> Answer:
        if self.confidence is not None and not is_probability(self.confidence):
            raise ValueError(f"confidence {self.confidence!r} is outside [0, 1]")
        for key, p in self.probabilities.items():
            if not is_probability(p):
                raise ValueError(f"probability for {key!r} is outside [0, 1]: {p!r}")
        if self.probabilities:
            total = sum(self.probabilities.values())
            tol = probability_sum_tolerance(self.probabilities)
            if abs(total - 1.0) > tol:
                raise ValueError(f"probabilities sum to {total:.4f}, expected 1 +/- {tol:.3f}")

        if self.type == "noul":
            if self.noul is None or not is_probability(self.noul):
                raise ValueError(f"noul answer needs a value in [0, 1], got {self.noul!r}")
            if self.choice is not None or self.score is not None:
                raise ValueError("noul answer must not set choice/score")
        elif self.type == "choice":
            if not self.choice:
                raise ValueError("choice answer needs a selected option")
            if self.noul is not None or self.score is not None:
                raise ValueError("choice answer must not set noul/score")
            if self.probabilities and self.choice not in self.probabilities:
                raise ValueError(f"selected {self.choice!r} is missing from probabilities")
        else:
            if self.score is None or not math.isfinite(self.score) or self.score < 0:
                raise ValueError(f"score answer needs a finite score >= 0, got {self.score!r}")
            if self.noul is not None or self.choice is not None:
                raise ValueError("score answer must not set noul/choice")
        return self


class DecisionResponse(BaseModel):
    """What a provider returns for one batch of questions."""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    #: the *resolved* model id (e.g. ``jev-1.13.0``), not the alias that was requested
    model: str
    answers: dict[str, Answer]
    latency_ms: float = Field(ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)
    #: only populated when raw persistence is explicitly enabled; may contain sensitive content
    raw: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _keys_match(self) -> DecisionResponse:
        for key, answer in self.answers.items():
            if answer.question_id != key:
                raise ValueError(f"answer keyed {key!r} claims question_id {answer.question_id!r}")
        return self


class ActionDecision(BaseModel):
    """The immutable result handed back to the host. The Reactor never executes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ActionName
    reason_codes: list[str] = Field(min_length=1)
    #: weakest link across the answers the winning rule consulted (see docs/policy-writing.md)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risk_score: float | None = Field(default=None, ge=0, le=1)
    target: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # traceability, filled in by the Reactor
    event_id: str | None = None
    sequence: int | None = None
    rule: str | None = None
    provider_status: ProviderStatus | None = None


class DecisionEvent(BaseModel):
    """One JSONL line: the event, what was asked, what came back, and what was decided."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    event: ReactorEvent
    question_pack_id: str
    question_pack_version: str
    #: hash of every question's wording; changes whenever behaviour could change
    question_pack_fingerprint: str | None = None
    policy_id: str | None = None
    response: DecisionResponse | None = None
    policy_result: ActionDecision
    provider_status: ProviderStatus
    #: wall-clock time from decide() start to decision, measured by the Reactor
    decision_latency_ms: float | None = None
    #: sha256 of the redacted state actually sent (state itself is not persisted by default)
    state_digest: str | None = None
    state_persisted: bool = False
    outcome: dict[str, Any] | None = None

    def to_jsonl(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> DecisionEvent:
        return cls.model_validate_json(line)


_PATH_RE = re.compile(r"`([A-Za-z_][\w]*(?:(?:\.[A-Za-z_][\w]*)|(?:\[\d+\]))*)`")


def referenced_paths(instructions: Any) -> set[str]:
    """Backticked dot/index paths mentioned in a question's instructions."""
    return set(_PATH_RE.findall(canonical_json(instructions).replace('\\"', '"')))

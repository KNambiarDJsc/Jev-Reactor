"""Versioned question packs, their linter, and response validation.

A pack is a repeatable decision task: a stable id, a version, atomic questions, the state
paths those questions refer to, and documented threshold defaults. Changing a question is
a behaviour change, so every recorded event carries the pack's *fingerprint* and replay
reports when it differs.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jev_reactor.errors import InvalidAnswerError, PackError
from jev_reactor.models import (
    ActionName,
    DecisionResponse,
    QuestionSpec,
    ReactorEvent,
    canonical_json,
    referenced_paths,
    sha256_hex,
)
from jev_reactor.state import default_state, path_declared

StateBuilderFn = Callable[[ReactorEvent], dict[str, Any]]


class PackExample(BaseModel):
    """A scenario shipped with the pack: a fixture file and the action policy should return."""

    model_config = ConfigDict(extra="forbid")

    name: str
    fixture: str | None = None
    expect_action: ActionName | None = None
    note: str = ""


class QuestionPack(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: str = Field(min_length=1)
    version: str = "1"
    description: str = ""
    questions: dict[str, QuestionSpec]
    #: state paths the questions may refer to (dot/index notation), checked by the linter
    state_paths: list[str] = Field(default_factory=list)
    #: documented defaults; the policy owns the values it actually applies
    thresholds: dict[str, Any] = Field(default_factory=dict)
    examples: list[PackExample] = Field(default_factory=list)
    #: code, not data: turns an event into the state sent to the provider
    state_builder: StateBuilderFn | None = Field(default=None, exclude=True, repr=False)
    #: list-valued top-level state keys (oldest first) that may be trimmed to fit
    trim_lists: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_match(self) -> QuestionPack:
        if not self.questions:
            raise ValueError("a pack needs at least one question")
        for key, spec in self.questions.items():
            if spec.id != key:
                raise ValueError(f"question keyed {key!r} declares id {spec.id!r}")
        return self

    def build_state(self, event: ReactorEvent) -> dict[str, Any]:
        return (self.state_builder or default_state)(event)

    def fingerprint(self) -> str:
        """Hash of everything that can change what Jev is asked."""
        payload = {
            "id": self.id,
            "version": self.version,
            "questions": {
                qid: {
                    "type": q.type,
                    "instructions": q.instructions,
                    "criteria": q.criteria,
                    "version": q.version,
                }
                for qid, q in sorted(self.questions.items())
            },
        }
        return sha256_hex(payload)

    # ------------------------------------------------------------------ YAML

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "version": self.version}
        if self.description:
            data["description"] = self.description
        data["state"] = {"paths": list(self.state_paths), "trim_lists": list(self.trim_lists)}
        data["questions"] = {
            qid: {
                "type": q.type,
                "version": q.version,
                "tags": list(q.tags),
                "instructions": q.instructions,
                **({"criteria": q.criteria} if q.criteria is not None else {}),
            }
            for qid, q in self.questions.items()
        }
        if self.thresholds:
            data["thresholds"] = self.thresholds
        if self.examples:
            data["examples"] = [e.model_dump(exclude_none=True) for e in self.examples]
        return data

    def to_yaml(self, header: str = "") -> str:
        body = yaml.safe_dump(
            self.to_dict(), sort_keys=False, allow_unicode=True, width=96, default_flow_style=False
        )
        return f"{header}{body}"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QuestionPack:
        if not isinstance(data, Mapping):
            raise PackError("a pack file must contain a mapping at the top level")
        data = dict(data)
        state = data.pop("state", None) or {}
        if not isinstance(state, Mapping):
            raise PackError("`state` must be a mapping with a `paths` list")
        questions = {}
        for qid, raw in (data.pop("questions", None) or {}).items():
            if not isinstance(raw, Mapping):
                raise PackError(f"question {qid!r} must be a mapping")
            questions[qid] = QuestionSpec(id=qid, **raw)
        return cls(
            questions=questions,
            state_paths=list(state.get("paths", data.pop("state_paths", []))),
            trim_lists=list(state.get("trim_lists", [])),
            **data,
        )


def load_pack(path: str | Path) -> QuestionPack:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackError(f"cannot read pack file {p}: {exc.strerror or exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PackError(f"{p} is not valid YAML: {exc}") from exc
    try:
        return QuestionPack.from_dict(data)
    except ValidationError as exc:
        raise PackError(f"{p}: " + "; ".join(_format_errors(exc))) from exc
    except (TypeError, ValueError) as exc:
        raise PackError(f"{p}: {exc}") from exc


def _format_errors(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "pack"
        out.append(f"{loc}: {err['msg']}")
    return out


# ---------------------------------------------------------------------------- linter

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class LintIssue:
    code: str
    severity: Severity
    message: str
    question_id: str | None = None

    def __str__(self) -> str:
        where = f" [{self.question_id}]" if self.question_id else ""
        return f"{self.severity.upper():7} {self.code}{where}: {self.message}"


_PLACEHOLDER = re.compile(r"\{\{?\s*[\w.]+\s*\}\}?|%\(\w+\)s|%s")
_DEGREE_ONLY = re.compile(
    r"^\s*(none|low|medium|moderate|mild|high|severe|very high|very low|minor|major)\.?\s*$", re.I
)
_BARE_NUMBER = re.compile(r"^\s*\d+(\.\d+)?\s*$")
_BROAD = re.compile(
    r"best (course of )?action"
    r"|decide (the )?(entire|whole|overall)"
    r"|what should (the )?(agent|system|we) do",
    re.I,
)
_NEGATED_TRUE = re.compile(r"^\s*(no|not|never|none)\b", re.I)


def lint_pack(pack: QuestionPack) -> list[LintIssue]:
    """Static checks that encode TypeSafe's guidance and independent measurements.

    Each rule cites why it exists in ``docs/question-packs.md``.
    """
    issues: list[LintIssue] = []

    def add(code: str, sev: Severity, msg: str, qid: str | None = None) -> None:
        issues.append(LintIssue(code, sev, msg, qid))

    seen: dict[str, str] = {}
    for qid, q in pack.questions.items():
        text = q.instructions if isinstance(q.instructions, str) else canonical_json(q.instructions)

        if q.type == "choice" and not q.abstain_options and "closed-set" not in q.tags:
            add(
                "choice-missing-abstain",
                "error",
                "Choice has no abstain option (other / none / unclear / unknown). Without one "
                "the model is forced to pick and stays confident: an independent audit measured "
                "accuracy dropping from 0.95 to 0.00 on unanswerable items. Add one, or tag the "
                "question `closed-set` if every input is guaranteed to fit an option.",
                qid,
            )
        if q.type == "score":
            levels = q.criteria or []
            texts = [lv if isinstance(lv, str) else canonical_json(lv) for lv in levels]
            if all(_BARE_NUMBER.match(t) for t in texts):
                add(
                    "score-numeric-levels",
                    "error",
                    "Score levels are bare numbers. The model sees only each level's "
                    "description, so numbers give it nothing to match against.",
                    qid,
                )
            elif any(_DEGREE_ONLY.match(t) for t in texts):
                add(
                    "score-degree-levels",
                    "warning",
                    "Score levels describe a degree ('low', 'high'). Describe situations "
                    "instead: 'Broken feature, but a workaround exists'.",
                    qid,
                )
        if q.type == "noul" and isinstance(q.criteria, dict):
            true_text = q.criteria.get("true")
            if isinstance(true_text, str) and _NEGATED_TRUE.match(true_text):
                add(
                    "noul-criteria-inverted",
                    "warning",
                    "Noul `true` description reads as a negative. Contradictory instructions "
                    "and criteria measurably hurt accuracy; make `true` mean yes.",
                    qid,
                )
        if _PLACEHOLDER.search(text):
            add(
                "instruction-placeholder",
                "warning",
                "Instructions contain a template placeholder. Put values from code in their "
                "own structured field instead of splicing them into a string.",
                qid,
            )
        if text.count("?") > 1:
            add(
                "instruction-compound",
                "warning",
                "Instructions contain more than one question. Split it: one judgment per question.",
                qid,
            )
        if _BROAD.search(text):
            add(
                "instruction-broad",
                "warning",
                "Instructions ask for an overall best action. Ask the specific judgments and "
                "combine them in code.",
                qid,
            )
        if len(text) > 600:
            add(
                "instruction-long",
                "warning",
                "Instructions are over 600 characters; keep them short.",
                qid,
            )

        if pack.state_paths:
            for ref in sorted(referenced_paths(q.instructions)):
                if not path_declared(ref, pack.state_paths):
                    add(
                        "unresolved-state-path",
                        "error",
                        f"`{ref}` is not declared under state.paths, so the state builder "
                        "may not provide it.",
                        qid,
                    )
        key = canonical_json({"i": q.instructions, "c": q.criteria, "t": q.type})
        if key in seen:
            add("duplicate-question", "error", f"identical to question {seen[key]!r}", qid)
        seen[key] = qid
    return issues


def lint_examples(pack: QuestionPack, base_dir: Path) -> list[LintIssue]:
    """Check that fixture files named by the pack's examples exist."""
    issues = []
    for ex in pack.examples:
        if ex.fixture and not (base_dir / ex.fixture).is_file():
            issues.append(
                LintIssue("example-fixture-missing", "error", f"{ex.name}: {ex.fixture} not found")
            )
    return issues


# ---------------------------------------------------------------------------- response check


def validate_response(response: DecisionResponse, questions: Mapping[str, QuestionSpec]) -> None:
    """Verify a response matches the questions that were asked. Never clamps or repairs."""
    missing = sorted(set(questions) - set(response.answers))
    if missing:
        raise InvalidAnswerError(f"provider omitted answers for: {', '.join(missing)}")
    extra = sorted(set(response.answers) - set(questions))
    if extra:
        raise InvalidAnswerError(
            f"provider returned answers for unasked questions: {', '.join(extra)}"
        )

    for qid, spec in questions.items():
        ans = response.answers[qid]
        if ans.type != spec.type:
            raise InvalidAnswerError(f"{qid}: expected a {spec.type} answer, got {ans.type}")
        if spec.type == "choice":
            options = set(spec.options)
            if ans.choice not in options:
                raise InvalidAnswerError(f"{qid}: choice {ans.choice!r} is not a declared option")
            unknown = set(ans.probabilities) - options
            if unknown:
                raise InvalidAnswerError(
                    f"{qid}: probabilities for undeclared options {sorted(unknown)}"
                )
        elif spec.type == "score":
            top = spec.level_count - 1
            if ans.score is None or ans.score > top + 1e-6:
                raise InvalidAnswerError(f"{qid}: score {ans.score} is outside 0..{top}")
            levels = {str(i) for i in range(spec.level_count)}
            unknown = (set(ans.probabilities) | set(ans.legend)) - levels
            if unknown:
                raise InvalidAnswerError(f"{qid}: unknown score levels {sorted(unknown)}")

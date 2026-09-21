"""Deterministic provider for tests, tutorials, and credential-free demos."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jev_reactor.errors import ProviderError, ProviderTimeoutError, ReactorError
from jev_reactor.models import Answer, DecisionResponse, QuestionSpec, canonical_json

AnswerLike = Any  # float | (choice, conf) | (score, conf) | dict | Answer
Rule = tuple[Callable[[Any], bool], Mapping[str, AnswerLike]]


def estimate_tokens(value: Any) -> int:
    """Cheap, provider-independent token estimate (about four characters per token)."""
    text = value if isinstance(value, str) else canonical_json(value)
    return max(1, math.ceil(len(text) / 4))


def probabilities_for_confidence(
    options: Sequence[str], choice: str, confidence: float
) -> dict[str, float]:
    """Distribution whose docs-formula confidence equals ``confidence``.

    TypeSafe describes confidence as a statistic of the distribution, e.g.
    ``(3 * p_max - 1) / 2`` for three options, i.e. ``(n * p_max - 1) / (n - 1)``.
    Inverting it keeps mock answers internally consistent.
    """
    n = len(options)
    p_max = (confidence * (n - 1) + 1) / n
    rest = (1 - p_max) / (n - 1)
    probs = {o: round(rest, 4) for o in options}
    probs[choice] = round(1 - sum(v for o, v in probs.items() if o != choice), 4)
    return probs


def score_probabilities(score: float, levels: int) -> dict[str, float]:
    """Mass on the two levels around ``score`` so the expectation equals ``score``."""
    lo = min(math.floor(score), levels - 1)
    hi = min(lo + 1, levels - 1)
    p_hi = round(score - lo, 4) if hi != lo else 0.0
    probs = {str(i): 0.0 for i in range(levels)}
    probs[str(hi)] = p_hi
    probs[str(lo)] = round(1 - p_hi, 4)
    return probs


def build_answer(spec: QuestionSpec, value: AnswerLike) -> Answer:
    """Turn a compact description into a validated Answer for ``spec``."""
    if isinstance(value, Answer):
        return value
    qid = spec.id
    if spec.type == "noul":
        if isinstance(value, Mapping):
            value = value.get("noul", value.get("p"))
        return Answer(question_id=qid, type="noul", noul=float(value))
    if spec.type == "choice":
        if isinstance(value, Mapping):
            choice, conf = value["choice"], value.get("confidence", 0.9)
            probs = value.get("probabilities") or probabilities_for_confidence(
                spec.options, choice, conf
            )
        else:
            choice, conf = value
            probs = probabilities_for_confidence(spec.options, choice, conf)
        return Answer(
            question_id=qid, type="choice", choice=choice, confidence=conf, probabilities=probs
        )
    if isinstance(value, Mapping):
        score, conf = value["score"], value.get("confidence", 0.9)
        probs = value.get("probabilities") or score_probabilities(score, spec.level_count)
    else:
        score, conf = value
        probs = score_probabilities(score, spec.level_count)
    legend = {str(i): d for i, d in enumerate(spec.criteria or [])}
    return Answer(
        question_id=qid,
        type="score",
        score=float(score),
        confidence=conf,
        probabilities=probs,
        legend=legend,
    )


@dataclass
class MockCall:
    state: Any
    question_ids: list[str]
    timeout: float


@dataclass
class Fixture:
    """A recorded scenario: an event, the answers Jev gave, and what policy should decide."""

    name: str
    description: str
    event: dict[str, Any]
    answers: dict[str, AnswerLike]
    expected: dict[str, Any]
    model: str = "mock-1.0"
    latency_ms: float = 12.0
    path: Path | None = None


def load_fixture(path: str | Path) -> Fixture:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        resp = data.get("response", {})
        return Fixture(
            name=data.get("name", p.stem),
            description=data.get("description", ""),
            event=data["event"],
            answers=resp.get("answers", data.get("answers", {})),
            expected=data.get("expected", {}),
            model=resp.get("model", "mock-1.0"),
            latency_ms=float(resp.get("latency_ms", 12.0)),
            path=p,
        )
    except (OSError, KeyError, ValueError) as exc:
        raise ReactorError(f"could not load fixture {p}: {exc}") from exc


class MockProvider:
    """Answers from fixed answers, first-match rules, or a callable. No network, no key.

    ``latency`` simulates provider time; ``raises`` scripts failures, one entry per call
    (``None`` means succeed). ``respect_timeout=False`` lets a test prove that the
    Reactor's own deadline, not the provider's, is what fires.
    """

    def __init__(
        self,
        answers: Mapping[str, AnswerLike] | None = None,
        *,
        rules: Sequence[Rule] = (),
        respond: Callable[[Any, dict[str, QuestionSpec]], Mapping[str, AnswerLike]] | None = None,
        model: str = "mock-1.0",
        latency: float = 0.0,
        reported_latency_ms: float | None = None,
        raises: Sequence[BaseException | None] = (),
        respect_timeout: bool = True,
    ) -> None:
        self.answers = dict(answers or {})
        self.rules = list(rules)
        self.respond = respond
        self.model = model
        self.latency = latency
        self.reported_latency_ms = reported_latency_ms
        self._raises = list(raises)
        self.respect_timeout = respect_timeout
        self.calls: list[MockCall] = []

    @classmethod
    def from_fixture(
        cls, path: str | Path, *, simulate_latency: bool = False, **kwargs: Any
    ) -> MockProvider:
        """Answer from a fixture. The recorded latency is *reported*, and only slept if asked."""
        fx = load_fixture(path)
        return cls(
            fx.answers,
            model=fx.model,
            latency=fx.latency_ms / 1000.0 if simulate_latency else 0.0,
            reported_latency_ms=fx.latency_ms,
            **kwargs,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _pick(self, state: Any, questions: dict[str, QuestionSpec]) -> Mapping[str, AnswerLike]:
        if self.respond is not None:
            return self.respond(state, questions)
        for predicate, answers in self.rules:
            if predicate(state):
                return answers
        return self.answers

    async def evaluate(
        self,
        *,
        state: str | dict[str, Any] | list[Any],
        questions: dict[str, QuestionSpec],
        timeout: float,
    ) -> DecisionResponse:
        self.calls.append(MockCall(state, list(questions), timeout))
        scripted = self._raises.pop(0) if self._raises else None

        if self.latency:
            if self.respect_timeout and self.latency > timeout:
                await asyncio.sleep(timeout)
                raise ProviderTimeoutError(f"mock provider exceeded {timeout:.3f}s")
            await asyncio.sleep(self.latency)
        if scripted is not None:
            raise scripted

        chosen = self._pick(state, questions)
        answers: dict[str, Answer] = {}
        for qid, spec in questions.items():
            if qid in chosen:  # missing answers are left out on purpose: the Reactor must notice
                try:
                    answers[qid] = build_answer(spec, chosen[qid])
                except (ValueError, TypeError, KeyError) as exc:
                    raise ProviderError(f"mock answer for {qid!r} is invalid: {exc}") from exc
        return DecisionResponse(
            request_id=f"mock-{len(self.calls)}",
            model=self.model,
            answers=answers,
            latency_ms=(
                self.reported_latency_ms
                if self.reported_latency_ms is not None
                else self.latency * 1000.0
            ),
            usage={"input_tokens": estimate_tokens(state), "output_tokens": 0},
        )

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jev_reactor.models import (
    ActionDecision,
    Answer,
    DecisionResponse,
    QuestionSpec,
    probability_sum_tolerance,
    referenced_paths,
)


def test_noul_answer_has_no_confidence_requirement() -> None:
    a = Answer(question_id="q", type="noul", noul=0.91)
    assert a.confidence is None


@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan"), float("inf")])
def test_noul_out_of_range_is_rejected_not_clamped(bad: float) -> None:
    with pytest.raises(ValidationError):
        Answer(question_id="q", type="noul", noul=bad)


def test_score_probabilities_and_legend_are_stringified() -> None:
    # the SDK hands back int keys; persisted JSON must be stable
    a = Answer(
        question_id="s",
        type="score",
        score=1.43,
        confidence=0.35,
        probabilities={0: 0.0, 1: 0.57, 2: 0.43},  # type: ignore[dict-item]
        legend={0: "a", 1: "b", 2: "c"},  # type: ignore[dict-item]
    )
    assert list(a.probabilities) == ["0", "1", "2"]
    assert list(a.legend) == ["0", "1", "2"]


def test_two_decimal_rounding_is_tolerated() -> None:
    # three options at 0.33 sum to 0.99; a flat 1e-3 tolerance would wrongly reject this
    Answer(
        question_id="c",
        type="choice",
        choice="a",
        confidence=0.1,
        probabilities={"a": 0.33, "b": 0.33, "c": 0.33},
    )


def test_probabilities_that_do_not_sum_to_one_are_rejected() -> None:
    with pytest.raises(ValidationError, match="sum"):
        Answer(
            question_id="c",
            type="choice",
            choice="a",
            confidence=0.5,
            probabilities={"a": 0.5, "b": 0.2},
        )


def test_tolerance_is_capped() -> None:
    assert probability_sum_tolerance({str(i): 0.01 for i in range(100)}) == 0.05


def test_choice_must_be_in_its_probabilities() -> None:
    with pytest.raises(ValidationError, match="missing from probabilities"):
        Answer(
            question_id="c",
            type="choice",
            choice="z",
            confidence=0.5,
            probabilities={"a": 0.5, "b": 0.5},
        )


def test_answer_type_fields_are_exclusive() -> None:
    with pytest.raises(ValidationError):
        Answer(question_id="q", type="noul", noul=0.5, choice="a")


def test_response_keys_must_match_answer_ids() -> None:
    with pytest.raises(ValidationError):
        DecisionResponse(
            model="m",
            latency_ms=1,
            answers={"x": Answer(question_id="y", type="noul", noul=0.5)},
        )


def test_question_spec_criteria_rules() -> None:
    QuestionSpec(id="n", type="noul", instructions="Is it?", criteria={"true": "y", "false": "n"})
    with pytest.raises(ValidationError):
        QuestionSpec(id="n", type="noul", instructions="Is it?", criteria={"maybe": "?"})
    with pytest.raises(ValidationError):
        QuestionSpec(id="c", type="choice", instructions="Which?", criteria={"only": None})
    with pytest.raises(ValidationError):
        QuestionSpec(id="s", type="score", instructions="How?", criteria=["one level"])
    with pytest.raises(ValidationError):
        QuestionSpec(
            id="s", type="score", instructions="How?", criteria=[str(i) for i in range(11)]
        )
    with pytest.raises(ValidationError):
        QuestionSpec(id="q", type="noul", instructions="   ")


def test_abstain_options_are_detected() -> None:
    q = QuestionSpec(
        id="c", type="choice", instructions="Which?", criteria={"a": None, "b": None, "Other": None}
    )
    assert q.abstain_options == ["Other"]


def test_action_decision_is_immutable_and_needs_a_reason() -> None:
    d = ActionDecision(action="review", reason_codes=["x"])
    with pytest.raises(ValidationError):
        d.action = "allow"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ActionDecision(action="allow", reason_codes=[])
    with pytest.raises(ValidationError):
        ActionDecision(action="call_tool", reason_codes=["x"])  # type: ignore[arg-type]


def test_referenced_paths() -> None:
    paths = referenced_paths("Does `proposed_call.tool` match `recent_calls[0].result` for `goal`?")
    assert paths == {"proposed_call.tool", "recent_calls[0].result", "goal"}
    assert referenced_paths({"question": "Is `goal` done?", "compare": ["`a.b`"]}) == {
        "goal",
        "a.b",
    }

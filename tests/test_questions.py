from __future__ import annotations

from pathlib import Path

import pytest

from jev_reactor.errors import InvalidAnswerError, PackError
from jev_reactor.models import Answer, DecisionResponse, QuestionSpec
from jev_reactor.questions import QuestionPack, lint_pack, load_pack, validate_response


def spec(**kw: object) -> QuestionSpec:
    base: dict[str, object] = {"id": "q", "type": "noul", "instructions": "Is `goal` done?"}
    base.update(kw)
    return QuestionSpec(**base)  # type: ignore[arg-type]


def pack(*questions: QuestionSpec, paths: list[str] | None = None) -> QuestionPack:
    return QuestionPack(
        id="p",
        questions={q.id: q for q in questions},
        state_paths=paths if paths is not None else ["goal"],
    )


def codes(p: QuestionPack) -> set[str]:
    return {i.code for i in lint_pack(p)}


# ---------------------------------------------------------------------------- fingerprint


def test_fingerprint_changes_when_wording_changes_but_not_when_tags_change() -> None:
    a = pack(spec(instructions="Is `goal` done?"))
    b = pack(spec(instructions="Is `goal` finished?"))
    c = pack(spec(instructions="Is `goal` done?", tags=["x"]))
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint() == c.fingerprint()


def test_fingerprint_changes_when_criteria_or_version_change() -> None:
    base = spec(type="choice", instructions="Which?", criteria={"a": None, "other": None})
    other = spec(type="choice", instructions="Which?", criteria={"a": "x", "other": None})
    assert pack(base, paths=[]).fingerprint() != pack(other, paths=[]).fingerprint()
    assert pack(spec()).fingerprint() != pack(spec(version="2")).fingerprint()


# ---------------------------------------------------------------------------- lint


def test_clean_pack_has_no_issues() -> None:
    assert lint_pack(pack(spec())) == []


def test_choice_without_an_abstain_option_is_an_error() -> None:
    p = pack(spec(type="choice", instructions="Which?", criteria={"a": None, "b": None}), paths=[])
    issue = next(i for i in lint_pack(p) if i.code == "choice-missing-abstain")
    assert issue.severity == "error"
    assert "0.95" in issue.message  # cites the measured failure


def test_closed_set_tag_waives_the_abstain_rule() -> None:
    p = pack(
        spec(
            type="choice",
            instructions="Which?",
            criteria={"a": None, "b": None},
            tags=["closed-set"],
        ),
        paths=[],
    )
    assert "choice-missing-abstain" not in codes(p)


def test_numeric_score_levels_are_an_error_and_degree_levels_a_warning() -> None:
    numeric = pack(spec(type="score", instructions="How?", criteria=["0", "1", "2"]), paths=[])
    degree = pack(
        spec(type="score", instructions="How?", criteria=["low", "medium", "high"]), paths=[]
    )
    assert "score-numeric-levels" in codes(numeric)
    assert "score-degree-levels" in codes(degree)
    good = pack(
        spec(
            type="score",
            instructions="How?",
            criteria=["No workaround needed", "Workaround exists"],
        ),
        paths=[],
    )
    assert codes(good) == set()


def test_backticked_paths_must_be_declared() -> None:
    p = pack(spec(instructions="Is `missing.field` relevant to `goal`?"))
    unresolved = [i for i in lint_pack(p) if i.code == "unresolved-state-path"]
    assert len(unresolved) == 1 and "missing.field" in unresolved[0].message


def test_path_check_is_skipped_when_the_pack_declares_no_paths() -> None:
    assert "unresolved-state-path" not in codes(pack(spec(instructions="Is `x` ok?"), paths=[]))


def test_string_spliced_placeholders_are_flagged() -> None:
    for text in ("Is {tool} relevant?", "Is {{tool}} relevant?", "Is %s relevant?"):
        assert "instruction-placeholder" in codes(pack(spec(instructions=text), paths=[]))


def test_compound_and_broad_questions_are_flagged() -> None:
    assert "instruction-compound" in codes(
        pack(spec(instructions="Is it done? Is it safe?"), paths=[])
    )
    assert "instruction-broad" in codes(
        pack(spec(instructions="What is the best course of action?"), paths=[])
    )


def test_inverted_noul_criteria_and_duplicates() -> None:
    inverted = pack(
        spec(instructions="Is it safe?", criteria={"true": "Not safe at all", "false": "Safe"}),
        paths=[],
    )
    assert "noul-criteria-inverted" in codes(inverted)
    dup = QuestionPack(
        id="p",
        questions={
            "a": QuestionSpec(id="a", type="noul", instructions="Same?"),
            "b": QuestionSpec(id="b", type="noul", instructions="Same?"),
        },
    )
    assert "duplicate-question" in codes(dup)


# ---------------------------------------------------------------------------- yaml


def test_pack_round_trips_through_yaml(tmp_path: Path) -> None:
    original = pack(
        spec(instructions={"question": "Is `goal` done?", "compare": ["`goal`"]}),
        spec(id="c", type="choice", instructions="Which?", criteria={"a": "first", "other": None}),
    )
    path = tmp_path / "p.yaml"
    path.write_text(original.to_yaml(), encoding="utf-8")
    loaded = load_pack(path)
    assert loaded.fingerprint() == original.fingerprint()
    assert loaded.state_paths == original.state_paths


def test_load_pack_reports_clear_errors(tmp_path: Path) -> None:
    with pytest.raises(PackError, match="cannot read"):
        load_pack(tmp_path / "missing.yaml")
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(PackError, match="not valid YAML"):
        load_pack(bad_yaml)
    bad_type = tmp_path / "type.yaml"
    bad_type.write_text(
        "id: p\nquestions:\n  q:\n    type: rank\n    instructions: hi\n", encoding="utf-8"
    )
    with pytest.raises(PackError, match=r"questions\.q|type"):
        load_pack(bad_type)
    bad_choice = tmp_path / "choice.yaml"
    bad_choice.write_text(
        "id: p\nquestions:\n  q:\n    type: choice\n    instructions: pick\n"
        "    criteria: {only: null}\n",
        encoding="utf-8",
    )
    with pytest.raises(PackError, match="at least two options"):
        load_pack(bad_choice)


# ---------------------------------------------------------------------------- response check

QS = {
    "n": QuestionSpec(id="n", type="noul", instructions="Is it?"),
    "c": QuestionSpec(
        id="c", type="choice", instructions="Which?", criteria={"a": None, "other": None}
    ),
    "s": QuestionSpec(id="s", type="score", instructions="How?", criteria=["x", "y", "z"]),
}


def response(**answers: Answer) -> DecisionResponse:
    return DecisionResponse(model="m", latency_ms=1, answers=dict(answers))


def good() -> dict[str, Answer]:
    return {
        "n": Answer(question_id="n", type="noul", noul=0.5),
        "c": Answer(
            question_id="c",
            type="choice",
            choice="a",
            confidence=0.9,
            probabilities={"a": 0.95, "other": 0.05},
        ),
        "s": Answer(
            question_id="s",
            type="score",
            score=1.5,
            confidence=0.5,
            probabilities={"1": 0.5, "2": 0.5},
        ),
    }


def test_a_matching_response_validates() -> None:
    validate_response(response(**good()), QS)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda a: a.pop("n"), "omitted answers for: n"),
        (lambda a: a.update(extra=Answer(question_id="extra", type="noul", noul=0.1)), "unasked"),
        (
            lambda a: a.update(
                n=Answer(
                    question_id="n",
                    type="choice",
                    choice="a",
                    confidence=1,
                    probabilities={"a": 1.0},
                )
            ),
            "expected a noul",
        ),
        (
            lambda a: a.update(
                c=Answer(
                    question_id="c",
                    type="choice",
                    choice="zzz",
                    confidence=1,
                    probabilities={"zzz": 1.0},
                )
            ),
            "not a declared option",
        ),
        (
            lambda a: a.update(
                s=Answer(
                    question_id="s",
                    type="score",
                    score=2.6,
                    confidence=0.5,
                    probabilities={"2": 1.0},
                )
            ),
            "outside 0..2",
        ),
        (
            lambda a: a.update(
                s=Answer(
                    question_id="s",
                    type="score",
                    score=1.0,
                    confidence=0.5,
                    probabilities={"7": 1.0},
                )
            ),
            "unknown score levels",
        ),
    ],
)
def test_mismatched_responses_are_rejected(mutate: object, message: str) -> None:
    answers = good()
    mutate(answers)  # type: ignore[operator]
    with pytest.raises(InvalidAnswerError, match=message):
        validate_response(response(**answers), QS)

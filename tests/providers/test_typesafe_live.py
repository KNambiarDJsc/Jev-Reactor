"""Live smoke test. Never runs by default.

    TYPESAFE_LIVE_TESTS=1 TYPESAFE_API_KEY=... pytest -m live

It sends one benign request; it is not a benchmark and not a security probe.
"""

from __future__ import annotations

import os

import pytest

from jev_reactor.models import QuestionSpec
from jev_reactor.providers.typesafe import TypeSafeProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("TYPESAFE_LIVE_TESTS") != "1" or not os.environ.get("TYPESAFE_API_KEY"),
        reason="set TYPESAFE_LIVE_TESTS=1 and TYPESAFE_API_KEY to run live tests",
    ),
]


async def test_mixed_noul_choice_score_in_one_request() -> None:
    questions = {
        "billing": QuestionSpec(
            id="billing", type="noul", instructions="Is `ticket` about billing?"
        ),
        "tone": QuestionSpec(
            id="tone",
            type="choice",
            instructions="What is the tone of `ticket`?",
            criteria={"calm": None, "frustrated": None, "angry": None, "other": None},
        ),
        "urgency": QuestionSpec(
            id="urgency",
            type="score",
            instructions="How urgent is `ticket`?",
            criteria=["can wait", "this week", "today"],
        ),
    }
    async with TypeSafeProvider() as provider:
        result = await provider.evaluate(
            state={"ticket": "I was charged twice. Please fix this ASAP."},
            questions=questions,
            timeout=10.0,
        )
    assert set(result.answers) == set(questions)
    assert result.model.startswith("jev-")
    assert 0.0 <= result.answers["billing"].noul <= 1.0  # type: ignore[operator]

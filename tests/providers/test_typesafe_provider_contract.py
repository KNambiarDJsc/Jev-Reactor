"""Contract tests: the real ``typesafe-sdk`` runs against an in-process ``httpx2`` transport.

No network and no API key. These pin how the adapter converts questions and answers and
how every SDK error is translated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import pytest
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from jev_reactor.config import TypeSafeSettings
from jev_reactor.errors import (
    MissingApiKeyError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from jev_reactor.models import QuestionSpec
from jev_reactor.providers.typesafe import TypeSafeProvider, to_sdk_question

SECRET = "sk-test-SECRET-do-not-log"
STATE = {"goal": "find the invoice", "note": "PRIVATE-STATE-MARKER"}

QUESTIONS = {
    "should_call": QuestionSpec(
        id="should_call",
        type="noul",
        instructions="Should `proposed_call` run?",
        criteria={"true": "It helps.", "false": "It does not."},
    ),
    "next": QuestionSpec(
        id="next",
        type="choice",
        instructions="What next?",
        criteria={"call_tool": "Run it.", "other": None},
    ),
    "value": QuestionSpec(
        id="value", type="score", instructions="How valuable?", criteria=["none", "some", "lots"]
    ),
}

OK_BODY = {
    "model": "jev-1.13.0",
    "answers": {
        "should_call": {"type": "noul", "noul": 0.91},
        "next": {
            "type": "choice",
            "choice": "call_tool",
            "confidence": 0.86,
            "probabilities": {"call_tool": 0.93, "other": 0.07},
        },
        "value": {
            "type": "score",
            "score": 1.43,
            "confidence": 0.35,
            "legend": {"0": "none", "1": "some", "2": "lots"},
            "probabilities": {"0": 0.0, "1": 0.57, "2": 0.43},
        },
    },
    "usage": {"input_tokens": 332, "output_tokens": 18},
}

Handler = Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]]


def make_provider(handler: Handler, *, max_retries: int = 0, **kwargs: Any) -> TypeSafeProvider:
    client = AsyncTypeSafeClient(
        api_key=SECRET,
        transport=httpx2.MockTransport(handler),
        retry=RetryPolicy(max_retries=0),
    )
    settings = TypeSafeSettings(api_key=SECRET, max_retries=max_retries)  # type: ignore[arg-type]
    return TypeSafeProvider(settings=settings, client=client, **kwargs)


def respond(status: int, body: dict[str, Any] | None = None, **headers: str) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json=body if body is not None else {"error": "x"}, headers=headers
        )

    return handler


async def call(provider: TypeSafeProvider, timeout: float = 1.0) -> Any:
    return await provider.evaluate(state=STATE, questions=QUESTIONS, timeout=timeout)


# --------------------------------------------------------------------------- request shape


async def test_one_request_carries_every_question_with_the_right_wire_shape() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers["authorization"],
                "body": json.loads(request.content),
            }
        )
        return httpx2.Response(200, json=OK_BODY, headers={"x-typesafe-request-id": "req_1"})

    result = await call(make_provider(handler))

    assert len(seen) == 1, "independent questions must share one request"
    req = seen[0]
    assert req["url"] == "https://api.typesafe.ai/v1/systemone"
    assert req["auth"] == f"Bearer {SECRET}"
    assert req["body"]["model"] == "jev-latest"
    assert req["body"]["state"] == STATE
    qs = req["body"]["questions"]
    assert qs["should_call"] == {
        "type": "noul",
        "instructions": "Should `proposed_call` run?",
        "criteria": {"true": "It helps.", "false": "It does not."},
    }
    assert qs["next"]["type"] == "choice"
    assert qs["next"]["criteria"] == {"call_tool": "Run it.", "other": None}
    assert qs["value"]["criteria"] == ["none", "some", "lots"]
    assert result.request_id == "req_1"


def test_noul_without_criteria_omits_them() -> None:
    q = to_sdk_question(QuestionSpec(id="n", type="noul", instructions="Is it?"))
    assert q.criteria is None


# --------------------------------------------------------------------------- response mapping


async def test_answers_are_converted_and_provenance_is_preserved() -> None:
    result = await call(make_provider(respond(200, OK_BODY, **{"x-typesafe-request-id": "req_9"})))
    assert result.model == "jev-1.13.0", "the resolved model id must be recorded, not the alias"
    assert result.request_id == "req_9"
    assert result.usage == {"input_tokens": 332, "output_tokens": 18}
    assert result.latency_ms >= 0
    assert result.answers["should_call"].noul == 0.91
    assert result.answers["should_call"].confidence is None
    assert result.answers["next"].choice == "call_tool"
    score = result.answers["value"]
    assert score.score == 1.43
    # the SDK re-keys Score probabilities by int; the internal model uses strings
    assert list(score.probabilities) == ["0", "1", "2"]
    assert score.legend["2"] == "lots"


async def test_raw_payload_is_not_kept_unless_asked() -> None:
    assert (await call(make_provider(respond(200, OK_BODY)))).raw is None
    kept = await call(make_provider(respond(200, OK_BODY), include_raw=True))
    assert kept.raw is not None and kept.raw["model"] == "jev-1.13.0"


# --------------------------------------------------------------------------- error translation


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (429, {"retry-after": "2"}, ProviderRateLimitedError),
        (500, {}, ProviderUnavailableError),
        (529, {}, ProviderUnavailableError),  # "overloaded" arrives as a 5xx
        (401, {}, ProviderAuthError),
        (403, {}, ProviderAuthError),
        (422, {}, ProviderRejectedError),
        (400, {}, ProviderRejectedError),
    ],
)
async def test_http_errors_map_to_project_exceptions(
    status: int, headers: dict[str, str], expected: type[ProviderError]
) -> None:
    with pytest.raises(expected) as info:
        await call(make_provider(respond(status, **headers)))
    assert info.value.status == status


async def test_rate_limit_exposes_retry_after_in_seconds() -> None:
    with pytest.raises(ProviderRateLimitedError) as info:
        await call(make_provider(respond(429, **{"retry-after": "2"})))
    assert info.value.retry_after == pytest.approx(2.0)
    with pytest.raises(ProviderRateLimitedError) as info:
        await call(make_provider(respond(429, **{"retry-after-ms": "1500"})))
    assert info.value.retry_after == pytest.approx(1.5)


async def test_auth_errors_open_the_breaker_immediately_and_rejections_do_not() -> None:
    assert ProviderAuthError.trips_immediately is True
    assert ProviderRejectedError.counts_toward_breaker is False
    assert ProviderResponseError.counts_toward_breaker is False
    assert ProviderUnavailableError.counts_toward_breaker is True


async def test_malformed_200_becomes_a_response_error() -> None:
    bad = {"model": "m", "answers": {"should_call": {"type": "noul", "noul": "high"}}, "usage": {}}
    with pytest.raises(ProviderResponseError, match=r"answers\.should_call\.noul"):
        await call(make_provider(respond(200, bad)))


async def test_out_of_range_probability_is_rejected_not_clamped() -> None:
    body = json.loads(json.dumps(OK_BODY))
    body["answers"]["should_call"]["noul"] = 1.7
    with pytest.raises(ProviderResponseError):
        await call(make_provider(respond(200, body)))


async def test_transport_failures_map_to_timeout_and_unavailable() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    with pytest.raises(ProviderTimeoutError):
        await call(make_provider(slow))
    with pytest.raises(ProviderUnavailableError):
        await call(make_provider(down))


async def test_asyncio_deadline_is_enforced_even_if_the_transport_hangs() -> None:
    async def hang(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json=OK_BODY)

    started = time.perf_counter()
    with pytest.raises(ProviderTimeoutError):
        await call(make_provider(hang), timeout=0.15)
    assert time.perf_counter() - started < 1.0


# --------------------------------------------------------------------------- retry budget


async def test_no_retries_by_default_so_one_failure_is_one_call() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(500, json={})

    with pytest.raises(ProviderUnavailableError):
        await call(make_provider(handler))
    assert calls == 1


async def test_sdk_retry_budget_is_bounded_by_the_deadline() -> None:
    # The SDK default would keep retrying for up to 30 s. The adapter passes
    # RetryPolicy(timeout=deadline), so a failing call cannot outlive the deadline.
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(500, json={})

    started = time.perf_counter()
    provider = make_provider(handler, max_retries=5)
    # give the *client* the same generous retry settings the SDK would use by default
    provider._client = AsyncTypeSafeClient(
        api_key=SECRET,
        transport=httpx2.MockTransport(handler),
        retry=RetryPolicy(max_retries=5, backoff_initial=0.3),
    )
    with pytest.raises(ProviderError):
        await call(provider, timeout=0.5)
    assert time.perf_counter() - started < 1.0
    assert calls >= 1


# --------------------------------------------------------------------------- secrets


def test_missing_api_key_fails_fast_with_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="TYPESAFE_API_KEY"):
        TypeSafeProvider(settings=TypeSafeSettings.from_env({}))


async def test_a_config_error_is_not_swallowed_as_a_provider_outage() -> None:
    from jev_reactor.errors import ConfigError
    from jev_reactor.policy import RuleChainPolicy
    from jev_reactor.providers.mock import MockProvider
    from jev_reactor.questions import QuestionPack
    from jev_reactor.reactor import Reactor

    pack = QuestionPack(
        id="p", questions={"q": QuestionSpec(id="q", type="noul", instructions="Is it?")}
    )
    reactor = Reactor(
        MockProvider(raises=[ConfigError("bad configuration")]),
        pack=pack,
        policy=RuleChainPolicy([], policy_id="p"),
    )
    with pytest.raises(ConfigError, match="bad configuration"):
        await reactor.decide({"state": {}})


async def test_neither_key_nor_state_leaks_into_logs_or_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    messages: list[str] = []
    for status in (200, 401, 422, 500, 429):
        body = OK_BODY if status == 200 else {"error": "bad", "echo": STATE["note"]}
        try:
            await call(make_provider(respond(status, body)))
        except ProviderError as exc:
            messages.append(str(exc))
    blob = "\n".join(messages + [r.getMessage() for r in caplog.records])
    assert SECRET not in blob
    assert "PRIVATE-STATE-MARKER" not in blob


def test_settings_repr_hides_the_key() -> None:
    settings = TypeSafeSettings.from_env({"TYPESAFE_API_KEY": SECRET})
    assert SECRET not in repr(settings)
    assert SECRET not in settings.model_dump_json()


def test_settings_from_env_reads_documented_variables() -> None:
    settings = TypeSafeSettings.from_env(
        {
            "TYPESAFE_API_KEY": SECRET,
            "TYPESAFE_DEFAULT_MODEL": "jev-1.13.0",
            "TYPESAFE_BASE_URL": "https://example.test",
            "JEV_TIMEOUT_SECONDS": "0.5",
        }
    )
    assert settings.model == "jev-1.13.0"
    assert settings.base_url == "https://example.test"
    assert settings.timeout_seconds == 0.5
    default = TypeSafeSettings.from_env({})
    assert default.model == "jev-latest" and default.timeout_seconds == 1.0


async def test_missing_request_id_header_is_tolerated() -> None:
    # behind a gateway the header may be absent; the SDK property raises in that case
    result = await call(make_provider(respond(200, OK_BODY)))
    assert result.request_id is None


def test_sdk_body_logging_is_pinned_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_LOG_LEVEL", raising=False)
    sdk_logger = logging.getLogger("typesafe_sdk")
    monkeypatch.setattr(sdk_logger, "level", logging.NOTSET)
    make_provider(respond(200, OK_BODY))
    assert sdk_logger.level == logging.INFO  # DEBUG (which logs bodies) is filtered out


def test_explicit_sdk_logger_configuration_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk_logger = logging.getLogger("typesafe_sdk")
    monkeypatch.setattr(sdk_logger, "level", logging.DEBUG)
    make_provider(respond(200, OK_BODY))
    assert sdk_logger.level == logging.DEBUG

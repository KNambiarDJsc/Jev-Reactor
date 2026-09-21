"""TypeSafe Jev provider: the only module that imports ``typesafe_sdk``.

Verified against ``typesafe-sdk`` 0.7.0 (see ``tests/providers/test_typesafe_provider_contract.py``,
which drives the real SDK through ``httpx2.MockTransport``, no network involved):

* ``AsyncTypeSafeClient.system_one(state, questions, *, model, retry, timeout, ...)``
* ``response.answers[...]`` holds ``NoulAnswer`` / ``ChoiceAnswer`` / ``ScoreAnswer``;
  Score ``probabilities`` and ``legend`` are keyed by **int** in the SDK.
* the SDK's default ``RetryPolicy`` budget is 30 s, so a call could outlive any interactive
  deadline. This adapter always passes an explicit policy bounded by the deadline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pydantic import ValidationError
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    RetryPolicy,
    Score,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeBadRequestError,
    TypeSafeError,
    TypeSafeInternalServerError,
    TypeSafeNotFoundError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
    TypeSafeUnprocessableEntityError,
)

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
from jev_reactor.models import Answer, DecisionResponse, QuestionSpec

logger = logging.getLogger("jev_reactor.providers.typesafe")

SdkQuestion = Noul | Choice | Score


def to_sdk_question(spec: QuestionSpec) -> SdkQuestion:
    """Convert an internal QuestionSpec to the SDK's question object."""
    if spec.type == "noul":
        return Noul(instructions=spec.instructions, criteria=spec.criteria)
    if spec.type == "choice":
        return Choice(
            instructions=spec.instructions, criteria=cast(Mapping[str, Any], spec.criteria)
        )
    return Score(instructions=spec.instructions, criteria=cast(Sequence[Any], spec.criteria))


def to_answer(question_id: str, sdk_answer: Any) -> Answer:
    """Convert an SDK answer to the internal model. Score keys become strings."""
    kind = sdk_answer.type
    if kind == "noul":
        return Answer(question_id=question_id, type="noul", noul=sdk_answer.noul)
    if kind == "choice":
        return Answer(
            question_id=question_id,
            type="choice",
            choice=sdk_answer.choice,
            confidence=sdk_answer.confidence,
            probabilities=dict(sdk_answer.probabilities),
        )
    if kind == "score":
        return Answer(
            question_id=question_id,
            type="score",
            score=sdk_answer.score,
            confidence=sdk_answer.confidence,
            probabilities=dict(sdk_answer.probabilities),
            legend=dict(sdk_answer.legend),
        )
    raise ProviderResponseError(f"answer {question_id!r} has unknown type {kind!r}")


def safe_request_id(response: Any) -> str | None:
    """``response.request_id`` raises when the header is missing (e.g. behind a gateway)."""
    try:
        value = response.request_id
    except TypeSafeError:
        return None
    return str(value) if value else None


def guard_sdk_logging() -> None:
    """Keep request/response bodies out of logs unless the user opted in.

    The SDK logs full bodies (which contain the state) at DEBUG on the ``typesafe_sdk``
    logger. Unless that logger was configured explicitly, pin it to INFO so a global
    DEBUG setting cannot leak state. Set ``TYPESAFE_LOG_LEVEL`` or configure the logger
    yourself to opt back in.
    """
    sdk_logger = logging.getLogger("typesafe_sdk")
    if sdk_logger.level == logging.NOTSET and not os.environ.get("TYPESAFE_LOG_LEVEL"):
        sdk_logger.setLevel(logging.INFO)
    elif sdk_logger.isEnabledFor(logging.DEBUG):
        logger.warning(
            "typesafe_sdk debug logging is on; the SDK logs request and response bodies, "
            "which contain your state"
        )


def translate_error(exc: BaseException) -> ProviderError:
    """Map SDK exceptions to project exceptions. Messages never include request bodies."""
    status = getattr(exc, "status", None)
    request_id = getattr(exc, "request_id", None)
    common: dict[str, Any] = {"status": status, "request_id": request_id}
    # order matters: TypeSafeAPITimeoutError subclasses TypeSafeAPIConnectionError, and
    # TypeSafeAPIResponseValidationError subclasses TypeSafeAPIError.
    if isinstance(exc, TypeSafeAPITimeoutError | TimeoutError):
        return ProviderTimeoutError("provider call timed out", **common)
    if isinstance(exc, TypeSafeAPIConnectionError):
        return ProviderUnavailableError("could not reach the provider", **common)
    if isinstance(exc, TypeSafeAPIResponseValidationError):
        return ProviderResponseError(
            f"provider response failed validation at {exc.field_path!r}", **common
        )
    if isinstance(exc, TypeSafeRateLimitError):
        retry_ms = exc.retry_after_ms
        return ProviderRateLimitedError(
            "provider rate limit exceeded",
            retry_after=(retry_ms / 1000.0) if retry_ms is not None else None,
            **common,
        )
    if isinstance(exc, TypeSafeAuthenticationError | TypeSafePermissionDeniedError):
        return ProviderAuthError(f"provider rejected credentials (HTTP {status})", **common)
    if isinstance(
        exc, TypeSafeBadRequestError | TypeSafeNotFoundError | TypeSafeUnprocessableEntityError
    ):
        return ProviderRejectedError(
            f"provider rejected the request (HTTP {status}); check the question pack", **common
        )
    if isinstance(exc, TypeSafeInternalServerError):
        return ProviderUnavailableError(f"provider server error (HTTP {status})", **common)
    if isinstance(exc, TypeSafeAPIError):
        if status is not None and status >= 500:
            return ProviderUnavailableError(f"provider server error (HTTP {status})", **common)
        return ProviderRejectedError(f"provider returned HTTP {status}", **common)
    if isinstance(exc, TypeSafeError):
        return ProviderError("provider SDK error", **common)
    return ProviderError(f"unexpected provider failure: {type(exc).__name__}", **common)


class TypeSafeProvider:
    """Evaluates all questions in one ``system_one`` request."""

    def __init__(
        self,
        *,
        settings: TypeSafeSettings | None = None,
        client: Any | None = None,
        include_raw: bool = False,
    ) -> None:
        self.settings = settings or TypeSafeSettings.from_env()
        if client is None and self.settings.api_key is None:
            raise MissingApiKeyError()  # a config error must not look like an outage
        self._client = client
        self._owns_client = client is None
        self.include_raw = include_raw
        self.model = self.settings.model
        guard_sdk_logging()

    def _get_client(self) -> Any:
        if self._client is None:
            key = self.settings.api_key
            if key is None:
                raise MissingApiKeyError()
            self._client = AsyncTypeSafeClient(
                api_key=key.get_secret_value(),
                model=self.settings.model,
                base_url=self.settings.base_url,
                timeout=self.settings.timeout_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> TypeSafeProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def evaluate(
        self,
        *,
        state: str | dict[str, Any] | list[Any],
        questions: dict[str, QuestionSpec],
        timeout: float,
    ) -> DecisionResponse:
        client = self._get_client()
        sdk_questions = {qid: to_sdk_question(q) for qid, q in questions.items()}
        retry = RetryPolicy(max_retries=self.settings.max_retries, timeout=timeout)
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout):
                response = await client.system_one(
                    state=state,
                    questions=sdk_questions,
                    model=self.model,
                    timeout=timeout,
                    retry=retry,
                )
        except ProviderError:
            raise
        except (TypeSafeError, TimeoutError) as exc:
            raise translate_error(exc) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0

        try:
            answers = {qid: to_answer(qid, a) for qid, a in response.answers.items()}
            return DecisionResponse(
                request_id=safe_request_id(response),
                model=response.model,
                answers=answers,
                latency_ms=latency_ms,
                usage={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
                raw=response.model_dump(mode="json") if self.include_raw else None,
            )
        except ValidationError as exc:
            # pydantic messages quote offending numbers, not state
            raise ProviderResponseError(
                f"provider answer failed validation: {exc.error_count()} error(s)",
                request_id=safe_request_id(response),
            ) from exc

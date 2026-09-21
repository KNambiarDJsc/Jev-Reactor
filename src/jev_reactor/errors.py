"""Project-specific exceptions.

Every provider failure is translated into a :class:`ProviderError` subclass so the
core runtime never depends on SDK exception types. No exception message in this
module may contain API keys or event state.
"""

from __future__ import annotations


class ReactorError(Exception):
    """Base class for every error raised by Jev Reactor."""


class ConfigError(ReactorError):
    """Invalid or missing configuration."""


class MissingApiKeyError(ConfigError):
    """TYPESAFE_API_KEY is not set."""

    def __init__(self) -> None:
        super().__init__(
            "TYPESAFE_API_KEY is not set. Export it (or put it in a local .env file) to call "
            "Jev, or run with --mock / MockProvider, which needs no credentials."
        )


class InvalidQuestionError(ReactorError):
    """A question specification is malformed."""


class InvalidAnswerError(ReactorError):
    """A provider answer does not match the question that produced it.

    Out-of-range values are rejected, never clamped: a clamped probability could turn a
    malformed response into a confident-looking decision.
    """


class PolicyError(ReactorError):
    """A policy raised, or returned something that is not a valid ActionDecision."""


class PackError(ReactorError):
    """A question pack file is invalid."""


class ReplayError(ReactorError):
    """A recorded event could not be parsed."""

    def __init__(self, message: str, *, line: int | None = None) -> None:
        self.line = line
        prefix = f"line {line}: " if line is not None else ""
        super().__init__(prefix + message)


class ProviderError(ReactorError):
    """A decision provider failed to produce a usable response."""

    kind: str = "provider_error"
    #: transient failures count toward the circuit breaker
    counts_toward_breaker: bool = True
    #: the breaker opens immediately (e.g. bad credentials will not fix themselves)
    trips_immediately: bool = False

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        #: seconds the provider asked us to wait, when it said so
        self.retry_after = retry_after


class ProviderTimeoutError(ProviderError):
    kind = "timeout"


class ProviderUnavailableError(ProviderError):
    """Connection failure or a 5xx response."""

    kind = "unavailable"


class ProviderRateLimitedError(ProviderError):
    kind = "rate_limited"


class ProviderAuthError(ProviderError):
    """401/403: retrying cannot help, so the breaker opens immediately."""

    kind = "auth"
    trips_immediately = True


class ProviderRejectedError(ProviderError):
    """400/404/422: our request was invalid. This is a bug in a pack, not an outage."""

    kind = "rejected"
    counts_toward_breaker = False


class ProviderResponseError(ProviderError):
    """The provider answered, but the body was malformed or did not match the questions."""

    kind = "malformed_response"
    counts_toward_breaker = False

"""The provider interface. The core runtime depends on this and nothing else.

Any object with a matching ``evaluate`` coroutine is a provider: the TypeSafe adapter,
the mock, a wrapper around the official System One Adapter (OpenAI/Anthropic-backed), or
a local model you trained on your own labels.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from jev_reactor.models import DecisionResponse, QuestionSpec


@runtime_checkable
class DecisionProvider(Protocol):
    async def evaluate(
        self,
        *,
        state: str | dict[str, Any] | list[Any],
        questions: dict[str, QuestionSpec],
        timeout: float,
    ) -> DecisionResponse:
        """Answer every question about ``state`` in a single request where possible.

        Implementations must respect ``timeout`` (seconds), raise the project's
        ``ProviderError`` subclasses rather than SDK exceptions, and never log ``state``.
        """
        ...

"""Decision providers."""

from __future__ import annotations

import os

from jev_reactor.providers.base import DecisionProvider
from jev_reactor.providers.mock import MockProvider

__all__ = ["DecisionProvider", "MockProvider", "auto_provider"]

MOCK_ENV = "JEV_REACTOR_MOCK"


def auto_provider(*, mock: bool | None = None) -> DecisionProvider:
    """A mock provider when asked (or when ``JEV_REACTOR_MOCK=1``), else TypeSafe Jev.

    The TypeSafe adapter is imported lazily so importing this package never needs the SDK.
    """
    if mock is None:
        mock = os.environ.get(MOCK_ENV, "").lower() in {"1", "true", "yes"}
    if mock:
        return MockProvider()
    from jev_reactor.providers.typesafe import TypeSafeProvider

    return TypeSafeProvider()

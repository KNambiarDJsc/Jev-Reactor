"""Jev Reactor: an open-source runtime for real-time typed decisions.

Jev judges; ordinary code decides; adapters execute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.2.0"

from jev_reactor.api import *  # noqa: F403
from jev_reactor.api import __all__ as _api_all

if TYPE_CHECKING:
    from jev_reactor.providers.typesafe import TypeSafeProvider

__all__ = [*_api_all, "TypeSafeProvider", "__version__"]


def __getattr__(name: str) -> Any:
    # the TypeSafe SDK is imported only if you ask for the TypeSafe provider
    if name == "TypeSafeProvider":
        from jev_reactor.providers.typesafe import TypeSafeProvider

        return TypeSafeProvider
    raise AttributeError(f"module 'jev_reactor' has no attribute {name!r}")

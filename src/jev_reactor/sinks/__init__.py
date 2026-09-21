"""Event sinks. A sink receives every :class:`DecisionEvent` the Reactor records."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jev_reactor.models import DecisionEvent


@runtime_checkable
class Sink(Protocol):
    async def emit(self, record: DecisionEvent) -> None: ...


class MemorySink:
    """Keeps records in a list. Useful in tests and notebooks."""

    def __init__(self) -> None:
        self.records: list[DecisionEvent] = []

    async def emit(self, record: DecisionEvent) -> None:
        self.records.append(record)


from jev_reactor.sinks.jsonl import JsonlSink  # noqa: E402
from jev_reactor.sinks.stdout import StdoutSink  # noqa: E402

__all__ = ["JsonlSink", "MemorySink", "Sink", "StdoutSink"]

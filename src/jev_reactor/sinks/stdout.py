"""Human-readable one-line-per-decision output."""

from __future__ import annotations

from rich.console import Console

from jev_reactor.models import DecisionEvent

_STYLE = {
    "allow": "green",
    "stop": "green",
    "skip": "yellow",
    "compact": "yellow",
    "route": "cyan",
    "review": "magenta",
    "fallback": "magenta",
    "block": "bold red",
}


class StdoutSink:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    async def emit(self, record: DecisionEvent) -> None:
        d = record.policy_result
        style = _STYLE.get(d.action, "white")
        conf = f"{d.confidence:.2f}" if d.confidence is not None else "-"
        ms = (
            f"{record.decision_latency_ms:.0f}ms" if record.decision_latency_ms is not None else "-"
        )
        self.console.print(
            f"[dim]#{record.event.sequence:<3}[/] [{style}]{d.action:<8}[/] "
            f"{', '.join(d.reason_codes)}  [dim]conf={conf} status={record.provider_status} {ms}[/]"
        )

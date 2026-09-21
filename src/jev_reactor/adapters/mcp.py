"""MCP-compatible tool gate: propose a tool call, get a decision, execute only if allowed.

This adapter speaks MCP's *shapes* (tool dicts with ``name``, ``description``, ``inputSchema``
and ``annotations``; results with ``content``) but does not import the ``mcp`` SDK. A real
protocol adapter is on the roadmap for 0.2. Nothing in this module executes a tool by itself:
the host passes an ``executor`` and the gate calls it only when the decision permits.

Security stance (see SECURITY.md):

* **A listed tool is not a trusted tool.** Only tools the host names in ``allow`` are included.
* **Server hints can only raise risk.** ``destructiveHint`` moves a tool to ``irreversible``;
  a tool the host calls ``read`` but the server marks ``readOnlyHint: false`` becomes ``write``.
  Hints never lower a tier and never make a tool ``idempotent``.
* **Descriptions and results are untrusted text.** They reach Jev only as data under fixed
  state paths, never inside the question instructions, and never as commands to this code.
* **Hard rules bind in every mode.** Your allowlist, permissions, denials, amount limits and
  the approval requirement for irreversible actions are your own declared constraints; no
  mode overrides them. Modes only relax *Jev-driven* verdicts:

  ============  ===========================================================
  ``observe``   only your hard rules bind; Jev's verdicts are recorded as
                ``would_execute_if_enforced`` (start here, calibrate, then tighten)
  ``guard``     hard rules, plus Jev's safety verdicts (``block``, ``review``)
  ``enforce``   everything except ``allow`` is held back (the default)
  ============  ===========================================================
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jev_reactor.models import ActionDecision
from jev_reactor.packs.tool_loop import Risk, ToolLoop, ToolSpec
from jev_reactor.policy import Policy
from jev_reactor.questions import QuestionPack
from jev_reactor.reactor import Reactor

GateMode = Literal["observe", "guard", "enforce"]

_RISK_ORDER: dict[str, int] = {"read": 0, "write": 1, "irreversible": 2}


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:10]}")


def tools_from_mcp(
    mcp_tools: Iterable[Mapping[str, Any]],
    allow: Mapping[str, Risk | Mapping[str, Any]],
) -> list[ToolSpec]:
    """Build the allowlist from an MCP ``tools/list`` result. Unlisted tools are dropped.

    ``allow`` maps a tool name to a risk tier (``"read"``, ``"write"``, ``"irreversible"``) or
    to a dict of ``ToolSpec`` fields (permission, idempotent, amount limits...). Whatever the
    server says, the host's entry wins; server annotations may only raise the risk tier.
    """
    specs: list[ToolSpec] = []
    for tool in mcp_tools:
        name = tool.get("name")
        if not isinstance(name, str) or name not in allow:
            continue
        entry = allow[name]
        fields: dict[str, Any] = {"risk": entry} if isinstance(entry, str) else dict(entry)
        risk: Risk = fields.get("risk", "write")
        ann = tool.get("annotations") or {}
        if ann.get("destructiveHint") is True and _RISK_ORDER[risk] < _RISK_ORDER["irreversible"]:
            risk = "irreversible"
        elif ann.get("readOnlyHint") is False and risk == "read":
            risk = "write"
        fields["risk"] = risk
        fields.setdefault("description", str(tool.get("description") or ""))
        specs.append(ToolSpec(name=name, **fields))
    return specs


@dataclass(frozen=True)
class GateVerdict:
    call: ToolCall
    decision: ActionDecision
    mode: GateMode
    #: whether the host should run the call under this gate's mode
    may_execute: bool
    #: what enforce mode would have done: lets observe mode report "would have blocked"
    would_execute_if_enforced: bool


@dataclass
class GuardedResult:
    verdict: GateVerdict
    executed: bool
    result: Any = None

    @property
    def action(self) -> str:
        return self.verdict.decision.action


Executor = Callable[[ToolCall], Awaitable[Any]]


def _as_mapping(result: Any) -> Mapping[str, Any]:
    """A tool result as a plain mapping: dicts as-is, SDK models via ``model_dump``."""
    if isinstance(result, Mapping):
        return result
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        dumped = dump(by_alias=True, mode="json")
        if isinstance(dumped, Mapping):
            return dumped
    return {"content": [{"type": "text", "text": str(result)}]}


def _excerpt(result: Any, limit: int = 500) -> str:
    """Flatten an MCP-style result to text for the trace. Bounded; treated as untrusted."""
    result = _as_mapping(result)
    if isinstance(result.get("content"), list):
        parts = [str(c.get("text", "")) for c in result["content"] if isinstance(c, Mapping)]
        text = " ".join(p for p in parts if p)
    else:
        text = str(result)
    return text if len(text) <= limit else text[:limit]


@dataclass
class GateSession:
    """One conversation: keeps the goal, permissions, trace and skip streak for you."""

    gate: ToolGate
    goal: str
    permissions: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    agent_context: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    consecutive_skips: int = 0
    history_max: int = 50
    stream_id: str = field(default_factory=lambda: f"gate_{uuid.uuid4().hex[:8]}")

    async def propose(self, call: ToolCall, *, approved: bool = False) -> GateVerdict:
        state: dict[str, Any] = {"recent_calls": list(self.history)}
        if self.agent_context:
            state["agent_context"] = self.agent_context
        event = self.gate.reactor.new_event(
            "tool_call_proposed",
            state,
            goal=self.goal,
            proposed_action={"tool": call.name, "arguments": call.arguments},
            metadata={
                "permissions": self.permissions,
                "denied_tools": self.denied_tools,
                "approved": approved,
                "consecutive_skips": self.consecutive_skips,
                "stream_id": self.stream_id,
                "call_id": call.call_id,
            },
        )
        decision = await self.gate.reactor.decide(event, self.gate.pack, self.gate.policy)
        verdict = self.gate.verdict(call, decision)
        # only a call that was really held back counts toward the suppression budget
        suppressed = decision.action == "skip" and not verdict.may_execute
        self.consecutive_skips = self.consecutive_skips + 1 if suppressed else 0
        return verdict

    def record(self, call: ToolCall, result: Any) -> None:
        """Add an executed call and its (untrusted, bounded) result to the trace."""
        view = _as_mapping(result)
        self.history.append(
            {
                "tool": call.name,
                "arguments": call.arguments,
                "ok": not bool(view.get("isError")),
                "result_excerpt": _excerpt(view),
            }
        )
        del self.history[: -self.history_max]

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        executor: Executor,
        approved: bool = False,
    ) -> GuardedResult:
        """Propose, then execute **only if** the verdict permits. A blocked call never runs."""
        call = ToolCall(name=name, arguments=dict(arguments or {}))
        verdict = await self.propose(call, approved=approved)
        if not verdict.may_execute:
            return GuardedResult(verdict, executed=False)
        result = await executor(call)
        self.record(call, result)
        return GuardedResult(verdict, executed=True, result=result)


class ToolGate:
    def __init__(
        self,
        reactor: Reactor,
        tools: Sequence[ToolSpec],
        *,
        mode: GateMode = "enforce",
        pack: QuestionPack | None = None,
        policy: Policy | None = None,
    ) -> None:
        loop = ToolLoop(tools)
        self.reactor = reactor
        self.tools = list(tools)
        self.mode: GateMode = mode
        self.pack = pack or loop.pack
        self.policy = policy or loop.policy

    def session(
        self,
        goal: str,
        *,
        permissions: Sequence[str] = (),
        denied_tools: Sequence[str] = (),
        agent_context: str | None = None,
    ) -> GateSession:
        return GateSession(
            gate=self,
            goal=goal,
            permissions=list(permissions),
            denied_tools=list(denied_tools),
            agent_context=agent_context,
        )

    def verdict(self, call: ToolCall, decision: ActionDecision) -> GateVerdict:
        enforced = decision.action == "allow"
        # decided by a hard rule before Jev was asked: binding in every mode
        hard_rule = decision.provider_status == "not_called"
        if hard_rule and decision.action in {"block", "review"}:
            may = False
        elif self.mode == "enforce":
            may = enforced
        elif self.mode == "guard":
            may = decision.action not in {"block", "review"}
        else:  # observe
            may = True
        return GateVerdict(
            call, decision, self.mode, may_execute=may, would_execute_if_enforced=enforced
        )


# ---------------------------------------------------------------------------- demo server


class FakeToolServer:
    """**DEMO ONLY.** An in-memory stand-in for an MCP server with safe fake tools.

    No network, no files, no real side effects: ``send_email`` appends to a list and
    ``refund_payment`` edits a dict. It exists so the gate can be demonstrated, and tested,
    without any credentials or risk. It is not an MCP server and must not be deployed.
    """

    INVOICES = {
        "INV-2041": {"status": "PAID", "amount": 480.00, "paid_on": "2026-09-03"},
        "INV-2042": {"status": "OPEN", "amount": 1200.00, "due": "2026-10-01"},
    }

    def __init__(self) -> None:
        self.executed: list[ToolCall] = []
        self.outbox: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        def tool(name: str, description: str, **ann: bool) -> dict[str, Any]:
            return {
                "name": name,
                "description": description,
                "inputSchema": {"type": "object"},
                "annotations": ann,
            }

        return [
            tool("search_invoices", "Search invoices by id or customer.", readOnlyHint=True),
            tool(
                "get_invoice_status", "Return the payment status of one invoice.", readOnlyHint=True
            ),
            tool("write_note", "Attach a note to an invoice.", readOnlyHint=False),
            tool(
                "send_email",
                "Send an email to a customer. (fake: appends to an outbox)",
                destructiveHint=True,
            ),
            tool(
                "refund_payment",
                "Refund a payment. (fake: edits an in-memory list)",
                destructiveHint=True,
            ),
        ]

    async def call(self, call: ToolCall) -> dict[str, Any]:
        self.executed.append(call)
        args = call.arguments
        text: str
        if call.name in {"search_invoices", "get_invoice_status"}:
            inv = args.get("invoice_id") or args.get("id")
            record = self.INVOICES.get(str(inv))
            text = f"{inv}: {record}" if record else f"no invoice {inv!r}"
        elif call.name == "write_note":
            text = "note saved"
        elif call.name == "send_email":
            self.outbox.append(dict(args))
            text = f"queued email to {args.get('to')} (demo outbox, nothing was sent)"
        elif call.name == "refund_payment":
            self.refunds.append(dict(args))
            text = f"refunded {args.get('amount')} (demo ledger)"
        else:
            return {
                "content": [{"type": "text", "text": f"unknown tool {call.name}"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": text}], "isError": False}

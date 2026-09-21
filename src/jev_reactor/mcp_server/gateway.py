"""The MCP gateway: a real MCP server that fronts real MCP servers and gates every call.

    MCP client --tools/call--> [ Jev Reactor gateway ] ---> downstream MCP server(s)
    (Claude, Cursor,             hard rules, then Jev,
     an agent)                   then your policy

What it does, in order, for every ``tools/call``:

1. the tool must be on the host allowlist (unlisted tools are neither listed nor callable);
2. arguments are checked against the downstream tool's own JSON Schema (an invalid call is
   returned as a tool error the model can read and fix);
3. the Reactor decides: hard rules first (permissions, denials, amount limits, approval for
   irreversible actions, exact duplicates), then Jev's typed judgments, then the policy;
4. only an allowed call is forwarded. A held-back call **never reaches the downstream server**
   and comes back as an ``isError`` result that says why, so the agent can adapt.

Trust model. The downstream server, its tool descriptions and its results are untrusted. The
*client* is trusted only to relay a human's answer, never to grant permissions: permissions,
denials, the goal and approval requirements come from the host's config, not from arguments.
Approval for irreversible actions is asked of the human through MCP elicitation, in both
protocol eras (a direct request on the handshake era; a multi-round-trip result on
2026-07-28), and the state carried between rounds is sealed by the SDK.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import jsonschema
import mcp.types as t
from mcp import Client, StdioServerParameters
from mcp.server import Server, ServerRequestContext
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity

from jev_reactor import __version__
from jev_reactor.adapters.mcp import GateSession, GateVerdict, ToolCall, ToolGate, tools_from_mcp
from jev_reactor.config import ReactorConfig
from jev_reactor.errors import ConfigError, ReactorError
from jev_reactor.mcp_server.config import (
    SUSPICIOUS_DESCRIPTION,
    DownstreamConfig,
    GatewayConfig,
    ToolRule,
)
from jev_reactor.packs.tool_loop import ToolSpec
from jev_reactor.providers.base import DecisionProvider
from jev_reactor.providers.mock import MockProvider
from jev_reactor.reactor import Reactor
from jev_reactor.redaction import Redactor
from jev_reactor.sinks import JsonlSink, Sink

logger = logging.getLogger("jev_reactor.mcp")

SESSION_META_KEY = "io.jev-reactor/session"
DECISION_META_KEY = "io.jev-reactor/decision"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
APPROVAL_KEY = "approval"
APPROVAL_STATE = "approval-v1:"
MODERN_ERA = "2026-07-28"

WITHHELD_DESCRIPTION = (
    "(description withheld: it read like instructions to the model rather than a description "
    "of the tool)"
)

#: what an agent is told when a call is held back. Deliberately free of thresholds and
#: probabilities, which would help an attacker tune an injection.
EXPLANATIONS: dict[str, str] = {
    "tool_not_allowlisted": "this tool is not allowed through this gateway",
    "malformed_proposal": "the call was malformed",
    "user_denied": "the user has denied this tool",
    "permission_missing": "the permission this tool needs has not been granted",
    "amount_exceeds_limit": "the amount is over the limit set for this tool",
    "amount_invalid": "the amount is missing or not a valid number",
    "irreversible_needs_approval": "this action cannot be undone and needs the user's approval",
    "exact_duplicate_call": "an identical call already succeeded; use its earlier result",
    "redundant_tool_call": "it would repeat information already retrieved; use the earlier result",
    "not_relevant": "it does not appear to help with the current goal",
    "needs_clarification": "a needed detail is missing; ask the user for it",
    "ambiguous_relevance": "it is unclear whether this call is needed; ask the user",
    "task_complete": "the task already looks complete; answer the user instead",
    "possible_prompt_injection": "the call was held for human review",
    "injection_uncertain": "the call was held for human review",
    "signals_disagree": "the call was held for review because the checks disagreed",
    "low_choice_confidence": "the call was held for a closer check",
    "suppression_budget_exhausted": "the call was held for review after several were skipped",
    "policy_error": "the call was held because the policy could not decide",
    "provider_timeout": "the decision service did not answer in time, so the call was not run",
    "provider_error": "the decision service was unavailable, so the call was not run",
    "provider_circuit_open": "the decision service is unavailable, so the call was not run",
    "state_too_large": "the request was too large to check safely",
}


class GatewayError(ReactorError):
    """The gateway could not start (for example a downstream server would not connect)."""


@dataclass
class ExposedTool:
    exposed: str
    server: str
    name: str
    tool: t.Tool
    description: str
    spec: ToolSpec


def downstream_target(cfg: DownstreamConfig) -> Any:
    """A ``StdioServerParameters`` for a command, or the URL. (Headers are handled by the
    runtime, which owns the HTTP client.)"""
    if cfg.command is not None:
        return StdioServerParameters(
            command=cfg.command, args=cfg.args, env=cfg.env or None, cwd=cfg.cwd
        )
    return cfg.url


def request_meta(ctx: ServerRequestContext[Any, Any]) -> Mapping[str, Any]:
    """The request's inbound ``_meta`` as a plain mapping (empty if none)."""
    return cast("Mapping[str, Any]", ctx.meta or {})


def client_can_elicit(ctx: ServerRequestContext[Any, Any]) -> bool:
    """Can this client be asked a question? Capabilities travel differently per era."""
    if ctx.protocol_version >= MODERN_ERA:
        caps = request_meta(ctx).get(CLIENT_CAPABILITIES_META_KEY) or {}
        return bool(caps.get("elicitation"))
    try:
        return bool(
            ctx.session.check_client_capability(
                t.ClientCapabilities(
                    elicitation=t.ElicitationCapability(form=t.FormElicitationCapability())
                )
            )
        )
    except Exception:
        return False


def explain(decision_reasons: list[str], action: str) -> str:
    for reason in decision_reasons:
        if reason in EXPLANATIONS:
            return EXPLANATIONS[reason]
    if any(r.startswith("uncertain_") for r in decision_reasons):
        return "the call was held for review because the request was ambiguous"
    if any(r.startswith("provider_") for r in decision_reasons):
        return "the decision service was unavailable, so the call was not run"
    return f"the call was held ({action})"


def decision_meta(verdict: GateVerdict) -> dict[str, Any]:
    d = verdict.decision
    return {
        "action": d.action,
        "reasons": list(d.reason_codes),
        "executed": verdict.may_execute,
        "mode": verdict.mode,
        "event_id": d.event_id,
    }


def error_result(text: str, meta: dict[str, Any] | None = None) -> t.CallToolResult:
    return t.CallToolResult(
        content=[t.TextContent(text=text)],
        is_error=True,
        meta={DECISION_META_KEY: meta} if meta else None,
    )


def validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> str | None:
    """A short, model-readable summary of what is wrong, or None. Downstream schemas are
    untrusted input too, so a schema we cannot use is skipped rather than fatal."""
    try:
        validator = jsonschema.validators.validator_for(
            dict(schema), default=jsonschema.Draft202012Validator
        )(dict(schema))
        errors = sorted(validator.iter_errors(dict(arguments)), key=lambda e: list(e.path))
    except Exception:
        logger.warning("could not validate arguments against a downstream schema; skipping")
        return None
    if not errors:
        return None
    parts = []
    for err in errors[:3]:
        where = ".".join(str(p) for p in err.path) or "arguments"
        parts.append(f"{where}: {err.message[:160]}")
    return "; ".join(parts)


class GatewayRuntime:
    """Downstream connections, the exposed tool table, the Reactor, and per-conversation
    sessions. Owned by the server's lifespan."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        provider: DecisionProvider,
        reactor_config: ReactorConfig | None = None,
        sinks: list[Sink] | None = None,
        targets: Mapping[str, Any] | None = None,
    ) -> None:
        self.cfg = config
        self._targets = dict(targets or {})
        self.reactor = Reactor(
            provider,
            config=reactor_config
            or ReactorConfig(
                deadline_seconds=config.deadline_seconds, persist_state=config.persist.state
            ),
            sinks=sinks or [],
        )
        self.clients: dict[str, Client] = {}
        self.tools: dict[str, ExposedTool] = {}
        self.hidden: list[str] = []
        self.gate: ToolGate | None = None
        self._sessions: OrderedDict[str, GateSession] = OrderedDict()
        self._stack = AsyncExitStack()
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self._redactor = Redactor()

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> GatewayRuntime:
        await self._stack.__aenter__()
        try:
            for name, dcfg in self.cfg.downstream.items():
                self.clients[name] = await self._connect(name, dcfg)
            await self.refresh_tools(force=True)
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.reactor.aclose()
        await self._stack.aclose()

    async def _connect(self, name: str, dcfg: DownstreamConfig) -> Client:
        target = self._targets.get(name)
        if target is None:
            target = downstream_target(dcfg)
            if dcfg.url is not None and dcfg.headers:
                import httpx2
                from mcp.client.streamable_http import streamable_http_client

                http = await self._stack.enter_async_context(
                    httpx2.AsyncClient(headers=dcfg.headers, timeout=dcfg.timeout_seconds)
                )
                target = streamable_http_client(dcfg.url, http_client=http)
        try:
            async with asyncio.timeout(dcfg.timeout_seconds):
                return await self._stack.enter_async_context(
                    Client(target, read_timeout_seconds=dcfg.timeout_seconds)
                )
        except Exception as exc:
            # never include the config: it may carry credentials in env or headers
            raise GatewayError(
                f"could not connect to downstream server {name!r} ({type(exc).__name__}). "
                "Check its command or url, and that it starts on its own."
            ) from exc

    # ------------------------------------------------------------------ tools

    async def refresh_tools(self, *, force: bool = False) -> None:
        async with self._lock:
            if (
                not force
                and self.tools
                and time.monotonic() - self._loaded_at < self.cfg.tools_cache_seconds
            ):
                return
            exposed: dict[str, ExposedTool] = {}
            hidden: list[str] = []
            for server, client in self.clients.items():
                for tool in await self._list_all(client):
                    name = self.cfg.exposed_name(server, tool.name)
                    rule = self.cfg.rule_for(name)
                    if rule is None:
                        hidden.append(name)
                        continue
                    description = self._describe(name, tool, rule)
                    if description is None:
                        hidden.append(name)
                        continue
                    exposed[name] = ExposedTool(
                        name,
                        server,
                        tool.name,
                        tool,
                        description,
                        self._spec(name, tool, rule, description),
                    )
            changed = {n: e.spec for n, e in exposed.items()} != {
                n: e.spec for n, e in self.tools.items()
            }
            self.tools, self.hidden, self._loaded_at = exposed, hidden, time.monotonic()
            if changed or self.gate is None:
                self.gate = ToolGate(
                    self.reactor, [e.spec for e in exposed.values()], mode=self.cfg.mode
                )
                for session in self._sessions.values():
                    session.gate = self.gate
            if hidden:
                logger.info(
                    "not exposing %d downstream tool(s) that are not allowlisted", len(hidden)
                )

    @staticmethod
    async def _list_all(client: Client) -> list[t.Tool]:
        tools: list[t.Tool] = []
        cursor: str | None = None
        for _ in range(50):  # bounded: a server that never ends its pages cannot hang us
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools
        raise GatewayError("a downstream server returned more than 50 pages of tools")

    def _describe(self, exposed: str, tool: t.Tool, rule: ToolRule) -> str | None:
        """The description we will show clients (and Jev). None means do not expose the tool."""
        text = rule.description if self.cfg.description_mode == "host" else (tool.description or "")
        text = text or ""
        candidates = f"{text}\n{tool.title or ''}"
        if SUSPICIOUS_DESCRIPTION.search(candidates):
            logger.warning("tool %r has a description that reads like instructions", exposed)
            if self.cfg.on_suspicious_description == "hide":
                return None
            if self.cfg.on_suspicious_description == "quarantine":
                return WITHHELD_DESCRIPTION
        if self.cfg.description_mode == "truncate" and len(text) > self.cfg.description_max_chars:
            text = text[: self.cfg.description_max_chars].rstrip() + " [truncated]"
        return text

    def _spec(self, exposed: str, tool: t.Tool, rule: ToolRule, description: str) -> ToolSpec:
        fields = rule.model_dump(exclude={"description"}, exclude_none=True)
        annotations = tool.annotations.model_dump(by_alias=True) if tool.annotations else {}
        # server hints may only RAISE a risk tier, never lower one or mark a tool idempotent
        (spec,) = tools_from_mcp(
            [{"name": exposed, "description": description, "annotations": annotations}],
            {exposed: fields},
        )
        return spec

    def listed_tools(self) -> list[t.Tool]:
        return [
            t.Tool(
                name=e.exposed,
                title=e.tool.title,
                description=e.description,
                input_schema=e.tool.input_schema,
                output_schema=e.tool.output_schema,
                annotations=e.tool.annotations,
            )
            for e in self.tools.values()
        ]

    # ------------------------------------------------------------------ sessions

    def session_key(self, ctx: ServerRequestContext[Any, Any]) -> str:
        """Which conversation is this call part of?

        ``ctx.session`` is a fresh object per request, so it cannot answer that. A stdio server
        has exactly one client, hence one conversation. Over HTTP a client may name its own
        conversation (``_meta`` key ``io.jev-reactor/session``) or use a handshake-era
        ``Mcp-Session-Id``; otherwise every caller shares one conversation, which is only
        correct for a single-tenant deployment (``require_session_key`` turns that off).
        """
        explicit = request_meta(ctx).get(SESSION_META_KEY)
        if isinstance(explicit, str) and 0 < len(explicit) <= 128:
            return f"meta:{explicit}"
        headers = getattr(ctx.request, "headers", None)
        session_id = headers.get("mcp-session-id") if headers is not None else None
        if session_id:
            return f"http:{session_id}"
        if self.cfg.require_session_key:
            raise ConfigError(
                f"this gateway requires a session key: send _meta[{SESSION_META_KEY!r}]"
            )
        return "default"

    def session(self, key: str) -> GateSession:
        existing = self._sessions.get(key)
        if existing is not None:
            self._sessions.move_to_end(key)
            return existing
        assert self.gate is not None
        session = self.gate.session(
            self.cfg.goal or self.cfg.agent_context or "",
            permissions=self.cfg.permissions,
            denied_tools=self.cfg.denied_tools,
            agent_context=self.cfg.agent_context,
        )
        session.history_max = self.cfg.history_max
        self._sessions[key] = session
        while len(self._sessions) > self.cfg.max_sessions:
            self._sessions.popitem(last=False)  # forget the least recently used conversation
        return session

    # ------------------------------------------------------------------ the gated call

    async def call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: t.CallToolRequestParams
    ) -> t.CallToolResult | t.InputRequiredResult:
        await self.refresh_tools()
        arguments = dict(params.arguments or {})
        try:
            key = self.session_key(ctx)
        except ConfigError as exc:
            return error_result(str(exc))
        session = self.session(key)
        call = ToolCall(name=params.name, arguments=arguments)

        exposed = self.tools.get(params.name)
        if exposed is not None:
            problem = validate_arguments(exposed.tool.input_schema, arguments)
            if problem:
                return error_result(f"Invalid arguments for {params.name}: {problem}")

        # the human's answer counts only if it comes back with the state this gateway sealed
        # for exactly this call (session + tool + arguments); anything else is "not asked yet"
        fingerprint = call_fingerprint(key, call)
        answer = approval_answer(params, fingerprint)  # True / False, or None if not asked yet
        verdict = await session.propose(call, approved=answer is True)

        if needs_approval(verdict) and answer is None and self.cfg.approvals.mode == "elicit":
            asked = await self._ask_approval(ctx, session, call, exposed, fingerprint)
            if isinstance(asked, t.InputRequiredResult):
                return asked
            if asked is True:
                verdict = await session.propose(call, approved=True)
            elif asked is False:
                answer = False

        if not verdict.may_execute:
            return self._held_back(verdict, declined=answer is False)
        if exposed is None:  # unreachable: hard rules block unlisted tools; belt and braces
            return error_result("unknown tool")

        result = await self._forward(exposed, arguments)
        session.record(call, result)
        logger.info(
            "gateway: %s -> %s (%s)",
            params.name,
            verdict.decision.action,
            ",".join(verdict.decision.reason_codes),
        )
        return t.CallToolResult(
            content=result.content,
            structured_content=result.structured_content,
            is_error=result.is_error,
            meta={DECISION_META_KEY: decision_meta(verdict)},
        )

    async def _forward(self, exposed: ExposedTool, arguments: dict[str, Any]) -> t.CallToolResult:
        dcfg = self.cfg.downstream[exposed.server]
        try:
            async with asyncio.timeout(dcfg.timeout_seconds):
                return await self.clients[exposed.server].call_tool(exposed.name, arguments)
        except TimeoutError:
            return error_result(f"The {exposed.server!r} server did not answer in time.")
        except Exception as exc:
            # a downstream failure is a tool error the model can read, never a gateway crash,
            # and never a stack trace or the downstream's own message
            logger.warning("downstream %r failed: %s", exposed.server, type(exc).__name__)
            return error_result(f"The {exposed.server!r} server failed to run {exposed.name!r}.")

    def _held_back(self, verdict: GateVerdict, *, declined: bool = False) -> t.CallToolResult:
        d = verdict.decision
        if declined:
            why = "the user did not approve this action"
        elif needs_approval(verdict) and self.cfg.approvals.mode == "deny":
            why = "this action cannot be undone, and this gateway does not allow it to run"
        elif needs_approval(verdict):
            why = "this action needs the user's approval, and the client could not ask for it"
        else:
            why = explain(list(d.reason_codes), d.action)
        text = (
            f"Not run: {why}. Do not retry this call unchanged. "
            "Use information you already have, ask the user, or choose a different step."
        )
        logger.info(
            "gateway: %s held back -> %s (%s)",
            verdict.call.name,
            d.action,
            ",".join(d.reason_codes),
        )
        return error_result(text, decision_meta(verdict))

    # ------------------------------------------------------------------ approvals

    async def _ask_approval(
        self,
        ctx: ServerRequestContext[Any, Any],
        session: GateSession,
        call: ToolCall,
        exposed: ExposedTool | None,
        fingerprint: str,
    ) -> bool | t.InputRequiredResult | None:
        """Ask the human. Returns True/False for a direct answer, an InputRequiredResult to
        hand back on the modern era, or None if this client cannot be asked."""
        if not client_can_elicit(ctx):
            return None
        message = self._approval_message(session, call, exposed)
        schema = {
            "type": "object",
            "properties": {
                "approve": {
                    "type": "boolean",
                    "title": "Approve",
                    "description": "Allow this action to run once.",
                }
            },
            "required": ["approve"],
        }
        if ctx.protocol_version >= MODERN_ERA:
            return t.InputRequiredResult(
                input_requests={
                    APPROVAL_KEY: t.ElicitRequest(
                        params=t.ElicitRequestFormParams(message=message, requested_schema=schema)
                    )
                },
                request_state=f"{APPROVAL_STATE}{fingerprint}",
            )
        try:
            result = await ctx.session.elicit_form(message=message, requested_schema=schema)
        except Exception as exc:
            logger.warning("could not ask for approval: %s", type(exc).__name__)
            return None
        return result.action == "accept" and (result.content or {}).get("approve") is True

    def _approval_message(
        self, session: GateSession, call: ToolCall, exposed: ExposedTool | None
    ) -> str:
        """Built by the gateway from the actual call: never from anything the agent wrote as
        prose. The arguments are shown as data, redacted and truncated."""
        shown, _ = self._redactor.redact(call.arguments)
        text = json.dumps(shown, default=str, ensure_ascii=False)
        if len(text) > 600:
            text = text[:600] + " ..."
        risk = exposed.spec.risk if exposed else "unknown"
        return (
            f"An agent wants to run '{call.name}' ({risk}: this cannot be undone).\n"
            f"Goal: {session.goal[:200]}\nArguments: {text}\n"
            "Approve this single action?"
        )


def needs_approval(verdict: GateVerdict) -> bool:
    return verdict.decision.reason_codes == ["irreversible_needs_approval"]


def call_fingerprint(session_key: str, call: ToolCall) -> str:
    """Identifies one exact proposed call, so an approval cannot be replayed onto another."""
    blob = json.dumps(
        [session_key, call.name, call.arguments], sort_keys=True, default=str, ensure_ascii=True
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def approval_answer(params: t.CallToolRequestParams, fingerprint: str) -> bool | None:
    """The human's answer on a retry, or None if none valid was given.

    The SDK's request-state boundary has already verified that ``request_state`` is a token
    this gateway sealed; here it must additionally name *this* call. A bare answer with no
    state, or state from a different call, is ignored (and the human is asked again).
    """
    if params.request_state != f"{APPROVAL_STATE}{fingerprint}":
        return None
    response = (params.input_responses or {}).get(APPROVAL_KEY)
    if response is None:
        return None
    action = getattr(response, "action", None)
    content = getattr(response, "content", None) or {}
    return bool(action == "accept" and content.get("approve") is True)


# ---------------------------------------------------------------------------- assembly

INSTRUCTIONS = (
    "The tools here are gated by Jev Reactor. A call may be held back: the result then has "
    "isError set and explains why. Do not retry a held-back call unchanged; use information "
    "you already have, ask the user, or pick a different step. Some actions need the user's "
    "approval, which you cannot grant yourself."
)


def build_provider(config: GatewayConfig) -> DecisionProvider:
    if config.provider.kind == "mock":
        answers = config.provider.answers or DEMO_ANSWERS
        return MockProvider(answers)
    from jev_reactor.config import TypeSafeSettings
    from jev_reactor.providers.typesafe import TypeSafeProvider

    settings = TypeSafeSettings.from_env()
    if config.provider.model:
        settings = settings.model_copy(update={"model": config.provider.model})
    settings = settings.model_copy(update={"timeout_seconds": config.deadline_seconds})
    return TypeSafeProvider(settings=settings)


#: DEMO ONLY: what a mock Jev says when a config asks for ``kind: mock`` and gives no answers.
DEMO_ANSWERS: dict[str, Any] = {
    "relevant": 0.96,
    "should_call": 0.94,
    "redundant": 0.04,
    "task_complete": 0.03,
    "needs_user_input": 0.02,
    "injection_suspected": 0.01,
    "next_action": ("call_tool", 0.91),
}


def build_gateway(
    config: GatewayConfig,
    *,
    provider: DecisionProvider | None = None,
    targets: Mapping[str, Any] | None = None,
    sinks: list[Sink] | None = None,
) -> Server[GatewayRuntime]:
    """Assemble the (unstarted) low-level MCP server. Nothing connects until it runs."""
    chosen = provider or build_provider(config)
    all_sinks = list(sinks or [])
    if config.persist.decisions:
        all_sinks.append(JsonlSink(config.persist.decisions))

    @asynccontextmanager
    async def lifespan(_server: Server[GatewayRuntime]) -> AsyncIterator[GatewayRuntime]:
        async with GatewayRuntime(
            config, provider=chosen, sinks=all_sinks, targets=targets
        ) as runtime:
            yield runtime

    async def on_list_tools(
        ctx: ServerRequestContext[GatewayRuntime], params: t.PaginatedRequestParams | None
    ) -> t.ListToolsResult:
        await ctx.lifespan_context.refresh_tools()
        return t.ListToolsResult(tools=ctx.lifespan_context.listed_tools())

    async def on_call_tool(
        ctx: ServerRequestContext[GatewayRuntime], params: t.CallToolRequestParams
    ) -> t.CallToolResult | t.InputRequiredResult:
        return await ctx.lifespan_context.call_tool(ctx, params)

    server: Server[GatewayRuntime] = Server(
        config.name,
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    # Seal and verify the state carried between approval rounds (the low-level tier does not
    # by default). A per-process key suits a single process, which this gateway is.
    server.middleware.append(
        RequestStateBoundary(
            RequestStateSecurity(keys=[os.urandom(32)]), default_audience=server.name
        )
    )
    return server


__all__ = [
    "GatewayError",
    "GatewayRuntime",
    "build_gateway",
    "build_provider",
]

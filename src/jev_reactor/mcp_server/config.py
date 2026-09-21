"""Gateway configuration (YAML). Host-declared and therefore trusted.

Everything that decides what an agent may do lives here, *not* in tool-call arguments: an
agent (or a compromised tool) can put anything in an argument, so permissions, denials,
approval requirements and the goal are never accepted from the caller.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jev_reactor.errors import ConfigError

Risk = Literal["read", "write", "irreversible"]
GateMode = Literal["observe", "guard", "enforce"]

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: patterns that suggest a tool description is addressing the model, not describing a tool
SUSPICIOUS_DESCRIPTION = re.compile(
    r"(?i)(ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules)"
    r"|disregard (all |any |the )?(previous|prior|above) "
    r"|do not (tell|inform|mention|reveal)( this)?( to)? the user"
    r"|<\s*important\s*>|<\s*system\s*>"
    r"|system prompt"
    r"|before (using|calling) this tool.{0,80}(read|send|open|fetch|include)"
    r"|exfiltrat|silently (send|forward|upload|copy))"
)


def expand_env(value: str, *, where: str) -> str:
    """Replace ``${VAR}`` from the environment. A missing variable is an error naming the
    variable, never printing any value."""

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"{where} refers to ${{{name}}}, which is not set in the environment")
        return os.environ[name]

    return _ENV_REF.sub(sub, value)


class DownstreamConfig(BaseModel):
    """One MCP server the gateway fronts: a local command (stdio) or a URL (Streamable HTTP)."""

    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    #: added on top of a minimal allow-listed environment; the child does NOT inherit yours
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    #: bound on one downstream call, including the connection
    timeout_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _exactly_one_transport(self) -> DownstreamConfig:
        if (self.command is None) == (self.url is None):
            raise ValueError("set exactly one of `command` (stdio) or `url` (Streamable HTTP)")
        if self.url is not None and not self.url.startswith(("http://", "https://")):
            raise ValueError("`url` must start with http:// or https://")
        return self


class ToolRule(BaseModel):
    """Host policy for one exposed tool. ``risk`` defaults to ``write``: reviewable, never
    silently allowed when Jev is unavailable."""

    model_config = ConfigDict(extra="forbid")

    risk: Risk = "write"
    requires_permission: str | None = None
    idempotent: bool = False
    amount_arg: str | None = None
    max_amount: float | None = None
    #: replaces the downstream description (see ``description_mode: host``)
    description: str | None = None


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["typesafe", "mock"] = "typesafe"
    model: str | None = None
    #: only for ``kind: mock``. Defaults to confident yes-everywhere answers: DEMO ONLY
    answers: dict[str, Any] | None = None


class PersistConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: append-only JSONL of every decision (redacted); omit to keep nothing
    decisions: str | None = None
    state: Literal["digest", "redacted"] = "digest"


class ApprovalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``elicit``: ask the human through the MCP client. ``deny``: irreversible tools are
    #: never runnable through this gateway. Approval is never read from tool arguments.
    mode: Literal["elicit", "deny"] = "elicit"


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "jev-reactor-gateway"
    mode: GateMode = "enforce"
    #: what the agent is trying to do. MCP's `tools/call` carries no goal, so the host
    #: declares one. Relevance and completion questions have nothing to judge without it.
    goal: str | None = None
    agent_context: str | None = None
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    deadline_seconds: float = Field(default=1.0, gt=0)
    downstream: dict[str, DownstreamConfig]
    #: the allowlist. Keys are *exposed* tool names; a tool not listed here is not exposed
    #: and cannot be called, whatever the downstream server offers.
    tools: dict[str, ToolRule | Risk]
    permissions: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    #: ``auto``: prefix ``server__`` only when more than one downstream server is configured
    prefix_tool_names: Literal["auto", "always", "never"] = "auto"
    description_mode: Literal["passthrough", "truncate", "host"] = "truncate"
    description_max_chars: int = Field(default=400, ge=40)
    on_suspicious_description: Literal["quarantine", "hide", "allow"] = "quarantine"
    tools_cache_seconds: float = Field(default=30.0, ge=0)
    approvals: ApprovalConfig = Field(default_factory=ApprovalConfig)
    persist: PersistConfig = Field(default_factory=PersistConfig)
    #: recent calls kept per conversation (Jev only ever sees the newest few)
    history_max: int = Field(default=50, ge=1)
    max_sessions: int = Field(default=256, ge=1)
    #: reject calls that carry no session key (HTTP multi-tenant hardening)
    require_session_key: bool = False

    @field_validator("downstream")
    @classmethod
    def _needs_a_server(cls, value: dict[str, DownstreamConfig]) -> dict[str, DownstreamConfig]:
        if not value:
            raise ValueError("configure at least one downstream server")
        for name in value:
            if "__" in name or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError(f"downstream name {name!r} may only use letters, digits, - and _")
        return value

    @model_validator(mode="after")
    def _needs_something_to_judge(self) -> GatewayConfig:
        if not (self.goal or self.agent_context):
            raise ValueError(
                "set `goal` or `agent_context`: MCP tool calls carry no goal, and without one "
                "Jev has nothing to judge relevance against"
            )
        return self

    def rule_for(self, exposed_name: str) -> ToolRule | None:
        entry = self.tools.get(exposed_name)
        if entry is None:
            return None
        return entry if isinstance(entry, ToolRule) else ToolRule(risk=entry)

    def exposed_name(self, server: str, tool: str) -> str:
        prefix = self.prefix_tool_names == "always" or (
            self.prefix_tool_names == "auto" and len(self.downstream) > 1
        )
        return f"{server}__{tool}" if prefix else tool


def load_gateway_config(path: str | Path) -> GatewayConfig:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read gateway config {p}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{p} must contain a mapping at the top level")
    return parse_gateway_config(raw, source=str(p))


def parse_gateway_config(raw: dict[str, Any], *, source: str = "config") -> GatewayConfig:
    # expand ${VAR} in downstream env/headers/args, where secrets legitimately live
    for name, server in (raw.get("downstream") or {}).items():
        if not isinstance(server, dict):
            continue
        for key in ("env", "headers"):
            values = server.get(key)
            if isinstance(values, dict):
                server[key] = {
                    k: expand_env(str(v), where=f"downstream.{name}.{key}.{k}")
                    for k, v in values.items()
                }
    try:
        return GatewayConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(x) for x in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{source}: {details}") from exc

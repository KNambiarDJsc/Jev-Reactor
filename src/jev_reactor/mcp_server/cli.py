"""``jev-reactor-mcp``: serve a gated MCP gateway over stdio or Streamable HTTP.

    jev-reactor-mcp init --demo -o gateway.yaml      # a config that fronts the safe demo server
    jev-reactor-mcp check gateway.yaml               # validate offline; nothing is started
    jev-reactor-mcp serve gateway.yaml               # stdio, for Claude Desktop / Code / Cursor
    jev-reactor-mcp serve gateway.yaml --transport http --port 8765

In stdio mode **stdout belongs to the protocol**: nothing here prints to it, and all logging
goes to stderr. HTTP mode listens on loopback only unless ``--allow-remote`` is given, which
also requires a bearer token (read from an environment variable, never from the command line).
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jev_reactor.errors import ReactorError
from jev_reactor.mcp_server.config import GatewayConfig, load_gateway_config
from jev_reactor.mcp_server.gateway import build_gateway

logger = logging.getLogger("jev_reactor.mcp")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


# ---------------------------------------------------------------------------- serving


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def configure_logging(verbose: bool) -> None:
    """stderr only: in stdio mode a stray byte on stdout corrupts the protocol."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # the HTTP client stack logs URLs and can log payloads at DEBUG; keep it quiet
    for noisy in ("httpx", "httpx2", "httpcore", "typesafe_sdk", "mcp", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def warn_if_demo(config: GatewayConfig) -> None:
    if config.provider.kind == "mock":
        logger.warning(
            "provider is 'mock': a scripted stand-in that approves nearly everything. "
            "DEMO ONLY. Use provider kind 'typesafe' for real decisions."
        )
    if config.mode != "enforce":
        logger.warning(
            "mode is %r: Jev's verdicts are recorded but do NOT stop calls "
            "(your permission and risk rules still do).",
            config.mode,
        )


async def serve_stdio(config: GatewayConfig) -> None:
    from mcp.server.stdio import stdio_server

    server = build_gateway(config)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


class BearerAuth:
    """ASGI wrapper: 401 unless the request carries the expected bearer token."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = b""
        for key, value in scope.get("headers", []):
            if key == b"authorization" and value[:7].lower() == b"bearer ":
                supplied = value[7:].strip()
                break
        if not hmac.compare_digest(supplied, self._token):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await self.app(scope, receive, send)


def build_http_app(
    config: GatewayConfig,
    *,
    host: str,
    port: int,
    token: str | None = None,
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
    path: str = "/mcp",
) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings

    server = build_gateway(config)
    hosts = [f"{h}:*" for h in ("127.0.0.1", "localhost", "[::1]")] if is_loopback(host) else []
    hosts += list(allowed_hosts)
    origins = [f"http://{h}:*" for h in ("127.0.0.1", "localhost", "[::1]")]
    origins += list(allowed_origins)
    app: Any = server.streamable_http_app(
        streamable_http_path=path,
        host=host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )
    return BearerAuth(app, token) if token else app


async def serve_http(
    config: GatewayConfig,
    *,
    host: str,
    port: int,
    token: str | None,
    allowed_hosts: Sequence[str],
    allowed_origins: Sequence[str],
) -> None:
    import uvicorn

    app = build_http_app(
        config,
        host=host,
        port=port,
        token=token,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    logger.info(
        "serving on http://%s:%d/mcp%s", host, port, " (bearer token required)" if token else ""
    )
    await uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
    ).serve()


# ---------------------------------------------------------------------------- templates

DEMO_TEMPLATE = """\
# Jev Reactor MCP gateway: DEMO. Fronts a safe fake invoice server with a scripted stand-in
# for Jev, so you can see the gateway work with no API key. Not for real use.
name: jev-reactor-gateway
mode: enforce
goal: "Find the payment status of invoice INV-2041 and tell the customer"

provider:
  kind: mock            # DEMO ONLY: a scripted stand-in that approves nearly everything

downstream:
  invoices:
    command: {python}
    args: ["-m", "jev_reactor.mcp_server.demo_server"]

tools:                  # the allowlist: anything not listed is invisible and uncallable
  search_invoices: {{risk: read, idempotent: true, requires_permission: "invoices:read"}}
  get_invoice_status: {{risk: read, requires_permission: "invoices:read"}}
  write_note: write
  send_email: {{risk: irreversible, requires_permission: "email:send"}}
  refund_payment:
    risk: irreversible
    requires_permission: "payments:refund"
    amount_arg: amount
    max_amount: 500

permissions: ["invoices:read", "email:send"]   # refunds are deliberately NOT granted
"""

REAL_TEMPLATE = """\
# Jev Reactor MCP gateway. Fronts your MCP server(s); every tool call is decided by hard rules,
# then Jev, then your policy, before it is forwarded. See docs/mcp.md.
name: jev-reactor-gateway
mode: guard             # observe = record only | guard = block the risky, allow the rest | enforce
goal: "Describe what the agent is trying to accomplish"   # MCP calls carry no goal; you declare it

provider:
  kind: typesafe        # needs TYPESAFE_API_KEY in the environment
deadline_seconds: 1.0   # Jev's answer budget per decision; on timeout the tier's fail-safe applies

downstream:
  myserver:
    command: npx
    args: ["-y", "@example/your-mcp-server"]
    env:
      API_TOKEN: "${{MYSERVER_API_TOKEN}}"    # read from your environment; never written here

tools:                  # the allowlist. Only what you list is exposed. Classify each one.
  list_things: {{risk: read, idempotent: true}}
  update_thing: write
  delete_thing: {{risk: irreversible, requires_permission: "things:delete"}}

permissions: []         # e.g. ["things:delete"]. Granted by YOU here, never by the agent
"""


def write_template(path: Path, *, demo: bool, force: bool) -> None:
    if path.exists() and not force:
        raise ReactorError(f"{path} already exists (use --force to overwrite)")
    template = DEMO_TEMPLATE if demo else REAL_TEMPLATE  # `{{`/`}}` are literal braces
    path.write_text(template.format(python=_yaml_quote(sys.executable)), encoding="utf-8")


def _yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------- commands


def describe(config: GatewayConfig) -> list[str]:
    lines = [
        f"gateway {config.name!r}: mode={config.mode}, provider={config.provider.kind}, "
        f"deadline={config.deadline_seconds}s",
        f"goal: {(config.goal or config.agent_context or '')[:120]}",
    ]
    for name, server in config.downstream.items():
        target = server.command or server.url
        lines.append(f"downstream {name!r}: {'stdio' if server.command else 'http'} -> {target}")
    for name in config.tools:
        rule = config.rule_for(name)
        assert rule is not None
        needs = f", needs {rule.requires_permission}" if rule.requires_permission else ""
        granted = (
            ""
            if rule.requires_permission is None
            else (
                " (granted)" if rule.requires_permission in config.permissions else " (NOT granted)"
            )
        )
        lines.append(f"  tool {name}: {rule.risk}{needs}{granted}")
    if not any(True for _ in config.tools):
        lines.append("  (no tools allowed: nothing would be exposed)")
    return lines


def _port(value: str) -> int:
    port = int(value)
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError("port must be 1-65535")
    return port


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jev-reactor-mcp",
        description="Serve an MCP gateway that decides every tool call before it runs.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging (to stderr)")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("config", type=Path)
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1", help="http only (default: loopback)")
    serve.add_argument("--port", type=_port, default=8765, help="http only")
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="listen on a non-loopback address (requires --token-env)",
    )
    serve.add_argument(
        "--token-env",
        metavar="VAR",
        help="name of an environment variable holding a bearer token clients must send",
    )
    serve.add_argument(
        "--allowed-host", action="append", default=[], help="extra Host header value"
    )
    serve.add_argument("--allowed-origin", action="append", default=[], help="extra Origin value")
    serve.add_argument(
        "--mode", choices=["observe", "guard", "enforce"], help="override the config"
    )

    check = sub.add_parser("check", help="validate a config offline; starts nothing")
    check.add_argument("config", type=Path)

    init = sub.add_parser("init", help="write a starter gateway config")
    init.add_argument("-o", "--output", type=Path, default=Path("gateway.yaml"))
    init.add_argument("--demo", action="store_true", help="front the safe demo server, mock Jev")
    init.add_argument("--force", action="store_true")
    return p


def run_serve(args: argparse.Namespace) -> int:
    config = load_gateway_config(args.config)
    if args.mode:
        config = config.model_copy(update={"mode": args.mode})
    token: str | None = None
    if args.token_env:
        token = os.environ.get(args.token_env)
        if not token:
            raise ReactorError(
                f"--token-env {args.token_env}: that environment variable is empty or unset"
            )
    if args.transport == "http" and not is_loopback(args.host):
        if not args.allow_remote:
            raise ReactorError(
                f"refusing to listen on {args.host!r}: the gateway runs your tools, so it binds "
                "to loopback unless you pass --allow-remote (and --token-env)"
            )
        if token is None:
            raise ReactorError(
                "--allow-remote requires --token-env: an open gateway runs tools for anyone"
            )
    warn_if_demo(config)
    try:
        if args.transport == "stdio":
            asyncio.run(serve_stdio(config))
        else:
            asyncio.run(
                serve_http(
                    config,
                    host=args.host,
                    port=args.port,
                    token=token,
                    allowed_hosts=args.allowed_host,
                    allowed_origins=args.allowed_origin,
                )
            )
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    configure_logging(args.verbose)
    try:
        if args.command == "serve":
            return run_serve(args)
        if args.command == "check":
            config = load_gateway_config(args.config)
            print("\n".join(describe(config)))
            print("config OK (nothing was started or contacted)")
            return 0
        write_template(args.output, demo=args.demo, force=args.force)
        print(f"wrote {args.output}")
        return 0
    except ReactorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BaseExceptionGroup as group:
        # startup failures inside the SDK's task groups arrive wrapped; show the real cause
        leaves = _leaves(group)
        print(f"error: {'; '.join(str(x) for x in leaves)}", file=sys.stderr)
        return 1


def _leaves(group: BaseException) -> list[BaseException]:
    if isinstance(group, BaseExceptionGroup):
        out: list[BaseException] = []
        for inner in group.exceptions:
            out.extend(_leaves(inner))
        return out
    return [group]


if __name__ == "__main__":
    raise SystemExit(main())

"""The gateway over real transports: a real subprocess on stdio and a real HTTP listener.

These are slower than the in-memory tests and prove different things: that stdout carries
only protocol bytes, that the demo server really is spawned as a child, and that the HTTP
guard rails (loopback, bearer token) hold on a live socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import mcp.types as t
import pytest
from mcp import Client, StdioServerParameters

from jev_reactor.mcp_server import cli
from jev_reactor.mcp_server.config import load_gateway_config

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


@pytest.fixture
def demo_config(tmp_path: Path) -> Path:
    path = tmp_path / "gateway.yaml"
    cli.write_template(path, demo=True, force=False)
    return path


def text(result: t.CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, t.TextContent))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------- templates


def test_the_demo_template_is_a_valid_config(demo_config: Path) -> None:
    config = load_gateway_config(demo_config)
    assert config.provider.kind == "mock" and set(config.tools) >= {"send_email", "refund_payment"}
    assert "refunds are deliberately NOT granted" in demo_config.read_text(encoding="utf-8")
    assert "payments:refund" not in config.permissions


def test_the_real_template_is_a_valid_config_and_never_holds_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYSERVER_API_TOKEN", "dummy")
    path = tmp_path / "g.yaml"
    cli.write_template(path, demo=False, force=False)
    config = load_gateway_config(path)
    assert config.provider.kind == "typesafe" and config.mode == "guard"
    assert "${MYSERVER_API_TOKEN}" in path.read_text(encoding="utf-8")


def test_init_does_not_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "g.yaml"
    target.write_text("keep me", encoding="utf-8")
    assert cli.main(["init", "-o", str(target)]) == 2
    assert target.read_text(encoding="utf-8") == "keep me"
    assert "already exists" in capsys.readouterr().err


def test_check_describes_the_gateway_and_flags_ungranted_permissions(
    demo_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check", str(demo_config)]) == 0
    out = capsys.readouterr().out
    assert "refund_payment: irreversible, needs payments:refund (NOT granted)" in out
    assert "nothing was started" in out


def test_check_reports_a_bad_config_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("downstream: {a: {command: x}}\ntools: {t: read}\n", encoding="utf-8")
    assert cli.main(["check", str(bad)]) == 2
    err = capsys.readouterr().err
    assert "goal" in err and "Traceback" not in err


# ---------------------------------------------------------------------------- serve guard rails


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.com"])
def test_a_non_loopback_listen_address_is_refused_without_allow_remote(
    demo_config: Path, host: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["serve", str(demo_config), "--transport", "http", "--host", host])
    assert code == 2 and "refusing to listen" in capsys.readouterr().err


def test_allow_remote_still_requires_a_token(
    demo_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        ["serve", str(demo_config), "--transport", "http", "--host", "0.0.0.0", "--allow-remote"]
    )
    assert code == 2 and "requires --token-env" in capsys.readouterr().err


def test_an_unset_token_variable_is_an_error_not_an_open_gateway(
    demo_config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GW_TOKEN", raising=False)
    code = cli.main(["serve", str(demo_config), "--transport", "http", "--token-env", "GW_TOKEN"])
    assert code == 2 and "GW_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
    ],
)
def test_loopback_detection(host: str, expected: bool) -> None:
    assert cli.is_loopback(host) is expected


# ---------------------------------------------------------------------------- stdio subprocess


async def test_the_gateway_runs_as_a_real_stdio_subprocess_and_stdout_is_pure_protocol(
    demo_config: Path,
) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jev_reactor.mcp_server.cli", "serve", str(demo_config)],
    )
    async with asyncio.timeout(60), Client(params) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "send_email" in names and "search_invoices" in names
        ok = await client.call_tool("search_invoices", {"invoice_id": "INV-2041"})
        assert not ok.is_error and "PAID" in text(ok)
        # refunds are not granted in the demo config: a hard rule holds it, in a real child
        held = await client.call_tool("refund_payment", {"payment_id": "P1", "amount": 10})
        assert held.is_error and "permission" in text(held)


# ---------------------------------------------------------------------------- HTTP


@contextlib.asynccontextmanager
async def http_gateway(config_path: Path, *, token: str | None = None) -> AsyncIterator[str]:
    import uvicorn

    port = free_port()
    config = load_gateway_config(config_path)
    app = cli.build_http_app(config, host="127.0.0.1", port=port, token=token)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "the HTTP gateway did not start"
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 15)


async def test_the_gateway_serves_streamable_http_on_loopback(demo_config: Path) -> None:
    async with asyncio.timeout(90), http_gateway(demo_config) as url, Client(url) as client:
        ok = await client.call_tool("get_invoice_status", {"id": "INV-2042"})
        assert not ok.is_error and "OPEN" in text(ok)
        held = await client.call_tool("refund_payment", {"payment_id": "P1", "amount": 10})
        assert held.is_error


async def test_a_bearer_token_is_enforced_on_the_wire(demo_config: Path) -> None:
    import httpx2

    async with asyncio.timeout(90), http_gateway(demo_config, token="s3cret-token") as url:
        async with httpx2.AsyncClient() as http:
            anon = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            wrong = await http.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer nope"},
            )
        assert anon.status_code == 401 and wrong.status_code == 401
        assert "s3cret-token" not in anon.text


async def test_a_foreign_host_header_is_rejected_by_dns_rebinding_protection(
    demo_config: Path,
) -> None:
    import httpx2

    async with (
        asyncio.timeout(90),
        http_gateway(demo_config) as url,
        httpx2.AsyncClient() as http,
    ):
        response = await http.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Host": "evil.example"},
        )
    assert response.status_code in (400, 403, 421)


def test_bearer_auth_wrapper_lets_lifespan_events_through() -> None:
    seen: list[str] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    wrapped = cli.BearerAuth(inner, "tok")
    asyncio.run(wrapped({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_bearer_auth_accepts_the_right_token_and_rejects_everything_else() -> None:
    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = cli.BearerAuth(inner, "tok")

    async def status(headers: list[tuple[bytes, bytes]]) -> int:
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await wrapped({"type": "http", "headers": headers}, None, send)
        return int(sent[0]["status"])

    assert asyncio.run(status([(b"authorization", b"Bearer tok")])) == 200
    assert asyncio.run(status([(b"authorization", b"bearer tok")])) == 200
    assert asyncio.run(status([(b"authorization", b"Bearer tokx")])) == 401
    assert asyncio.run(status([(b"authorization", b"Basic tok")])) == 401
    assert asyncio.run(status([])) == 401


def test_the_top_level_cli_delegates_mcp_subcommands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from typer.testing import CliRunner

    from jev_reactor.cli import app

    target = tmp_path / "g.yaml"
    runner = CliRunner()
    result = runner.invoke(app, ["mcp", "init", "--demo", "-o", str(target)])
    assert result.exit_code == 0 and target.exists()
    checked = runner.invoke(app, ["mcp", "check", str(target)])
    assert checked.exit_code == 0, checked.output

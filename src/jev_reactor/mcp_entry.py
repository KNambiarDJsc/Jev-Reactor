"""Console-script entry for ``jev-reactor-mcp``.

Kept outside ``jev_reactor.mcp_server`` so that a missing optional dependency produces one
clear line instead of a traceback: importing that package needs the MCP SDK.
"""

from __future__ import annotations

import sys


def main() -> None:
    try:
        from jev_reactor.mcp_server.cli import main as run
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(run())


if __name__ == "__main__":
    main()

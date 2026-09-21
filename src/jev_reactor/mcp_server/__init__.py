"""Real MCP servers built on the official ``mcp`` SDK (v2). Requires ``jev-reactor[mcp]``.

Nothing in the core package imports this subpackage, so ``import jev_reactor`` never loads
the MCP SDK.
"""

from __future__ import annotations

try:
    import mcp  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised by a subprocess test
    raise ImportError(
        "the MCP servers need the official MCP SDK. Install it with:\n"
        "    pip install 'jev-reactor[mcp]'"
    ) from exc

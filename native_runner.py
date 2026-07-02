"""Run one of the stack's MCP servers over Streamable HTTP.

Usage: python native_runner.py <module[:attr]> <port>

The server object (a FastMCP instance, attribute ``mcp`` by default) is served
stateless at http://127.0.0.1:<port>/mcp. Stateless keeps the governed path
simple: any client can POST a tools/call without a session handshake, and the
gateway can proxy calls without tracking per-client session state. The same
process serves mcpo (bridging to OpenAPI for Open WebUI) and native MCP
clients through the gateway's /{server}/mcp route.
"""
from __future__ import annotations

import importlib
import sys


def main() -> None:
    target, port = sys.argv[1], int(sys.argv[2])
    mod_name, _, attr = target.partition(":")
    server = getattr(importlib.import_module(mod_name), attr or "mcp")
    server.run(
        transport="http",
        show_banner=False,
        host="127.0.0.1",
        port=port,
        path="/mcp",
        stateless_http=True,
    )


if __name__ == "__main__":
    main()

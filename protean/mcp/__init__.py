"""Protean MCP server surface.

In-process Model Context Protocol server exposing Protean's Platform
abstractions to external CLI agents (claude_code, codex, ...) and via the
``protean mcp`` stdio CLI subcommand.

This is a top-level package so additional tool groups (e.g. recorder
control, skill management) can live alongside ``server.py`` without
crowding the executor namespace.
"""

from protean.mcp.server import build_mcp_server, result_to_mcp

__all__ = ["build_mcp_server", "result_to_mcp"]

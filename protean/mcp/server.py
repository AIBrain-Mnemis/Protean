"""In-process MCP tool surface exposing Platform methods to external CLI executors.

External CLI agents (claude_code, codex, ...) wrap a vendor SDK or CLI
that doesn't know about Protean's Platform layer. This module adapts
Platform methods into an MCP tool registry those agents can plug into
via ``create_sdk_mcp_server`` for in-process use, or via the CLI
subcommand ``protean mcp`` for stdio use.

The internal computer_use executor does NOT use this surface — it
drives the GUI through the vendor's Computer Use API and calls
Platform directly via the same ``ActionExecutor``.

Tool definitions come from ``GUI_TOOL_SPECS`` in
``protean.executor.actions``: this module is purely the MCP
wire-format adapter (``ActionResult`` → MCP content blocks) plus
error wrapping. ``ActionExecutor`` propagates ``Platform`` exceptions;
we catch here and format them with ``format_tool_error()`` (the same
schema + hint text the native computer-use loop gives the model) with
``is_error=True``.

Platform methods are called directly (not via asyncio.to_thread) because
Windows UIA uses COM objects that are apartment-threaded — calling them
from a thread-pool thread causes deadlocks.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from protean.executor.actions import (
    GUI_TOOL_SPECS,
    ActionExecutor,
    ActionResult,
    ToolSpec,
    format_tool_error,
)
from protean.platform.base import (
    LLM_SCREENSHOT_HEIGHT,
    LLM_SCREENSHOT_WIDTH,
    CoordinateMapper,
    Platform,
    active_display,
)

log = logging.getLogger(__name__)


def result_to_mcp(r: ActionResult) -> dict[str, Any]:
    """Translate a provider-neutral ActionResult into an MCP tool response.

    Success-path only. Errors are formatted by ``_error`` at the tool
    handler boundary so the host always sees ``is_error=True`` for
    Platform exceptions.
    """
    content: list[dict[str, Any]] = []
    if r.text:
        content.append({"type": "text", "text": r.text})
    if r.screenshot_b64:
        content.append({"type": "image", "data": r.screenshot_b64, "mimeType": "image/jpeg"})
    if r.detail_caption:
        content.append({"type": "text", "text": r.detail_caption})
    if r.detail_crop_b64:
        content.append({"type": "image", "data": r.detail_crop_b64, "mimeType": "image/jpeg"})
    if not content:
        # Defensive — always return at least one block so the MCP client
        # doesn't see an empty content array.
        content.append({"type": "text", "text": ""})
    return {"content": content}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def build_mcp_server(platform: Platform) -> Any:
    """Build an in-process MCP server wrapping Platform GUI methods.

    Returns an ``McpSdkServerConfig`` ready to pass into
    ``ClaudeAgentOptions.mcp_servers``. The returned config's ``instance``
    field is a standard ``mcp.server.Server`` usable with
    ``mcp.server.stdio.stdio_server()`` for the standalone
    ``protean mcp`` CLI subcommand.

    Every GUI tool from ``GUI_TOOL_SPECS`` is auto-registered via
    ``ActionExecutor.dispatch`` — there's no per-tool switch here, so
    adding a new GUI action in ``protean.executor.actions`` exposes
    it through MCP with no edits to this file.
    """
    display = active_display(platform)
    if display is None:
        raise RuntimeError("No display is available")
    mapper = CoordinateMapper(display, LLM_SCREENSHOT_WIDTH, LLM_SCREENSHOT_HEIGHT)
    actions = ActionExecutor(platform, mapper)

    all_tools = [_register_gui_tool(actions, spec) for spec in GUI_TOOL_SPECS]

    # Server name is the single source of truth for the wire-format
    # tool prefix (``mcp__protean__*``), the in-process dict key in
    # ``ClaudeAgentOptions.mcp_servers``, and the external-CLI config
    # key in ``mcp_servers.protean`` / ``mcpServers.protean``. Keeping
    # all three in sync is the whole point of having one constant.
    return create_sdk_mcp_server(
        name="protean",
        version="0.1.0",
        tools=all_tools,
    )


def _register_gui_tool(actions: ActionExecutor, spec: ToolSpec) -> Any:
    """Build one ``@tool``-decorated handler for ``spec``.

    Kept as a separate function so each handler closes over its own
    ``spec`` value (a naked ``for`` loop would let every handler share
    the loop variable's final value).
    """

    @tool(spec.name, spec.description, spec.input_schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            # ``include_screenshot`` is a batching hint from the calling
            # agent (see GUI_TOOL_SPECS), not a Platform action arg — pull
            # it out before dispatch instead of leaving it in the payload.
            include_screenshot = bool(args.pop("include_screenshot", True))
            result = await actions.dispatch(
                spec.name, args, include_screenshot=include_screenshot,
            )
            return result_to_mcp(result)
        except Exception as e:
            return _error(format_tool_error(spec.name, args, spec.input_schema, e))

    return handler

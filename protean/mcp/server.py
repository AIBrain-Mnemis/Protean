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
we catch here and produce ``"<Action> failed: {e}"`` text with
``is_error=True``, matching the legacy MCP behavior.

Platform methods are called directly (not via asyncio.to_thread) because
Windows UIA uses COM objects that are apartment-threaded — calling them
from a thread-pool thread causes deadlocks.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from protean.executor.actions import (
    GUI_TOOL_SPECS,
    ActionExecutor,
    ActionResult,
    ToolSpec,
)
from protean.platform.base import (
    LLM_SCREENSHOT_HEIGHT,
    LLM_SCREENSHOT_WIDTH,
    CoordinateMapper,
    Platform,
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


# JSON schema type → Python type for ``Annotated`` params expected by
# claude_agent_sdk's ``@tool`` decorator.
_JSON_TYPE_TO_PY: dict[str, type] = {
    "integer": int,
    "number": float,
    "string": str,
    "boolean": bool,
}


def _spec_to_annotated_schema(spec: ToolSpec) -> dict[str, Any]:
    """Convert a ``ToolSpec``'s JSON schema to ``@tool``-style Annotated dict.

    claude_agent_sdk's ``@tool`` wants ``{param: Annotated[type, desc]}``
    rather than a raw JSON schema; this is the bridge so the same
    ``GUI_TOOL_SPECS`` source feeds both Anthropic/OpenAI function
    tools (raw schema) and the MCP server (Annotated form).
    """
    props = spec.input_schema.get("properties", {})
    schema: dict[str, Any] = {}
    for param_name, prop in props.items():
        py_type = _JSON_TYPE_TO_PY.get(prop.get("type", "string"), str)
        schema[param_name] = Annotated[py_type, prop.get("description", "")]
    return schema


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
    mapper = CoordinateMapper(platform, LLM_SCREENSHOT_WIDTH, LLM_SCREENSHOT_HEIGHT)
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

    @tool(spec.name, spec.description, _spec_to_annotated_schema(spec))
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return result_to_mcp(await actions.dispatch(spec.name, args))
        except Exception as e:
            return _error(f"{spec.name} failed: {e}")

    return handler

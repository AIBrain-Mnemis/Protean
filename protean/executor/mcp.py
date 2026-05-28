"""In-process MCP tool surface exposing Platform methods to external CLI executors.

External CLI agents (claude_code, future codex, ...) wrap a vendor SDK or CLI
that doesn't know about Protean's Platform layer. This module adapts Platform
methods into an MCP tool registry those agents can plug into via
`create_sdk_mcp_server`, eliminating the need for an external MCP subprocess.

The internal computer_use executor does NOT use this surface — it drives the
GUI through the vendor's Computer Use API and calls Platform directly.

Platform methods are called directly (not via asyncio.to_thread) because
Windows UIA uses COM objects that are apartment-threaded — calling them
from a thread-pool thread causes deadlocks.
"""

from __future__ import annotations

import base64
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations as McpToolAnnotations

from protean.platform.base import (
    LLM_JPEG_QUALITY,
    LLM_SCREENSHOT_HEIGHT,
    LLM_SCREENSHOT_WIDTH,
    CoordinateMapper,
    Platform,
    parse_key_combo,
    prepare_screenshot_for_llm,
)

log = logging.getLogger(__name__)


def _take_screenshot(platform: Platform, mapper: CoordinateMapper) -> str:
    """Refresh the coord mapper, capture the active display, return base64 JPEG.

    Refreshing first ensures the same display we capture is the one whose
    scale will be used to translate subsequent click coordinates back.
    """
    mapper.refresh()
    tmp = Path(tempfile.gettempdir()) / f"protean_mcp_{time.monotonic_ns()}.png"
    try:
        platform.capture_display(mapper.display_index, tmp)
        raw_bytes = tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)
    jpeg_bytes, _ = prepare_screenshot_for_llm(
        raw_bytes,
        max_width=LLM_SCREENSHOT_WIDTH,
        max_height=LLM_SCREENSHOT_HEIGHT,
        quality=LLM_JPEG_QUALITY,
        exact_size=True,
    )
    return base64.b64encode(jpeg_bytes).decode("ascii")


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def _error(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _image(b64: str, mime: str = "image/jpeg") -> dict[str, Any]:
    return {"content": [{"type": "image", "data": b64, "mimeType": mime}]}


def build_mcp_server(platform: Platform) -> Any:
    """Build an in-process MCP server wrapping Platform GUI methods.

    Returns an McpSdkServerConfig ready to pass into ClaudeAgentOptions.mcp_servers.
    """

    mapper = CoordinateMapper(platform, LLM_SCREENSHOT_WIDTH, LLM_SCREENSHOT_HEIGHT)

    # -- screenshot ----------------------------------------------------------

    @tool(
        "screenshot",
        "Capture the current screen and return the image. "
        "The screenshot is taken from the display containing the active window.",
        {},
        annotations=McpToolAnnotations(maxResultSizeChars=500_000),
    )
    async def screenshot_tool(args: dict[str, Any]) -> dict[str, Any]:
        try:
            b64 = _take_screenshot(platform, mapper)
            return _image(b64)
        except Exception as e:
            return _error(f"Screenshot failed: {e}")

    # -- mouse actions -------------------------------------------------------

    @tool(
        "left_click",
        "Left-click at the given screen coordinates.",
        {"x": Annotated[int, "X coordinate"], "y": Annotated[int, "Y coordinate"]},
    )
    async def left_click_tool(args: dict[str, Any]) -> dict[str, Any]:
        ix, iy = int(args["x"]), int(args["y"])
        x, y = mapper.to_actual(ix, iy)
        try:
            platform.click(x, y)
            return _text(f"Clicked at ({ix}, {iy})")
        except Exception as e:
            return _error(f"Click failed: {e}")

    @tool(
        "right_click",
        "Right-click at the given screen coordinates.",
        {"x": Annotated[int, "X coordinate"], "y": Annotated[int, "Y coordinate"]},
    )
    async def right_click_tool(args: dict[str, Any]) -> dict[str, Any]:
        ix, iy = int(args["x"]), int(args["y"])
        x, y = mapper.to_actual(ix, iy)
        try:
            platform.click(x, y, "right")
            return _text(f"Right-clicked at ({ix}, {iy})")
        except Exception as e:
            return _error(f"Right-click failed: {e}")

    @tool(
        "double_click",
        "Double-click at the given screen coordinates.",
        {"x": Annotated[int, "X coordinate"], "y": Annotated[int, "Y coordinate"]},
    )
    async def double_click_tool(args: dict[str, Any]) -> dict[str, Any]:
        ix, iy = int(args["x"]), int(args["y"])
        x, y = mapper.to_actual(ix, iy)
        try:
            platform.double_click(x, y)
            return _text(f"Double-clicked at ({ix}, {iy})")
        except Exception as e:
            return _error(f"Double-click failed: {e}")

    @tool(
        "mouse_move",
        "Move the mouse cursor to the given screen coordinates without clicking.",
        {"x": Annotated[int, "X coordinate"], "y": Annotated[int, "Y coordinate"]},
    )
    async def mouse_move_tool(args: dict[str, Any]) -> dict[str, Any]:
        ix, iy = int(args["x"]), int(args["y"])
        x, y = mapper.to_actual(ix, iy)
        try:
            platform.move_cursor(x, y)
            return _text(f"Moved cursor to ({ix}, {iy})")
        except Exception as e:
            return _error(f"Move failed: {e}")

    # -- keyboard actions ----------------------------------------------------

    @tool(
        "type_text",
        "Type the given text at the current cursor position.",
        {"text": Annotated[str, "Text to type"]},
    )
    async def type_text_tool(args: dict[str, Any]) -> dict[str, Any]:
        text = str(args["text"])
        try:
            platform.type_text(text)
            return _text(f"Typed: {text[:80]}")
        except Exception as e:
            return _error(f"Type failed: {e}")

    @tool(
        "key_press",
        'Press a keyboard shortcut. Keys are joined by "+", '
        'e.g. "ctrl+c", "alt+tab", "enter", "ctrl+shift+s".',
        {"keys": Annotated[str, 'Key combination, e.g. "ctrl+c"']},
    )
    async def key_press_tool(args: dict[str, Any]) -> dict[str, Any]:
        keys_str = str(args["keys"])
        key_list = parse_key_combo(keys_str)
        if not key_list:
            return _error("No keys specified")
        try:
            platform.key_press(*key_list)
            return _text(f"Pressed: {keys_str}")
        except Exception as e:
            return _error(f"Key press failed: {e}")

    # -- scroll --------------------------------------------------------------

    @tool(
        "scroll",
        "Scroll at the given screen coordinates.",
        {
            "x": Annotated[int, "X coordinate"],
            "y": Annotated[int, "Y coordinate"],
            "direction": Annotated[str, 'Scroll direction: "up", "down", "left", or "right"'],
            "amount": Annotated[int, "Number of scroll steps (default 3)"],
        },
    )
    async def scroll_tool(args: dict[str, Any]) -> dict[str, Any]:
        ix, iy = int(args["x"]), int(args["y"])
        x, y = mapper.to_actual(ix, iy)
        direction = str(args.get("direction", "down"))
        amount = int(args.get("amount", 3))
        try:
            platform.scroll(x, y, direction, amount)
            return _text(f"Scrolled {direction} {amount} steps at ({ix}, {iy})")
        except Exception as e:
            return _error(f"Scroll failed: {e}")

    # -- UI element discovery ------------------------------------------------

    @tool(
        "find_elements",
        "Search for UI elements by text in the given application. "
        "Returns matching elements with their roles, labels, and center coordinates. "
        "Use the coordinates with left_click to interact with the element.",
        {
            "app": Annotated[str, "Application name or process name"],
            "query": Annotated[str, "Text to search for (case-insensitive)"],
        },
    )
    async def find_elements_tool(args: dict[str, Any]) -> dict[str, Any]:
        app = str(args["app"])
        query = str(args["query"])
        try:
            elements = platform.find_elements(app, query)
            if not elements:
                return _text(f"No elements found matching '{query}' in {app}")
            mapper.refresh()
            lines = []
            for i, el in enumerate(elements, 1):
                label = el.label[:80] if el.label else ""
                cx, cy = mapper.to_api(el.center_x, el.center_y)
                lines.append(
                    f"{i}. [{el.role}] \"{label}\" "
                    f"center=({cx}, {cy}) "
                    f"size={el.width}x{el.height}"
                )
            return _text("\n".join(lines))
        except Exception as e:
            return _error(f"find_elements failed: {e}")

    @tool(
        "list_elements",
        "List all visible UI elements in the given application's frontmost window. "
        "Returns a tree of elements with roles, labels, and positions.",
        {
            "app": Annotated[str, "Application name or process name"],
            "max_depth": Annotated[int, "Max tree depth (default 8, max 15)"],
        },
    )
    async def list_elements_tool(args: dict[str, Any]) -> dict[str, Any]:
        app = str(args["app"])
        max_depth = min(int(args.get("max_depth", 8)), 15)
        try:
            elements = platform.list_elements(app, max_depth)
            if not elements:
                return _text(f"No elements found in {app}")
            return _text("\n".join(elements))
        except Exception as e:
            return _error(f"list_elements failed: {e}")

    # -- app / window --------------------------------------------------------

    def _window_position_api(win: Any) -> tuple[int, int, int, int]:
        """Translate a window's real pixel rect into API space.

        Origin (x, y) goes through the full to_api transform (subtract
        display origin, scale). Width/height only get scaled — they're a
        size, not a point on a display.
        """
        x_api, y_api = mapper.to_api(win.x, win.y)
        scale = mapper.scale
        w_api = int(round(win.width * scale.api_w / scale.actual_w))
        h_api = int(round(win.height * scale.api_h / scale.actual_h))
        return x_api, y_api, w_api, h_api

    @tool(
        "activate_app",
        "Bring the given application to the foreground.",
        {"app": Annotated[str, "Application name, process name, or bundle ID"]},
    )
    async def activate_app_tool(args: dict[str, Any]) -> dict[str, Any]:
        app = str(args["app"])
        try:
            platform.activate_app(app)
            win = platform.get_active_window()
            if win:
                mapper.refresh()
                wx, wy, ww, wh = _window_position_api(win)
                return _text(
                    f"Activated {app}. Active window: "
                    f"\"{win.window_title}\" ({win.process_name}) "
                    f"at ({wx}, {wy}) size {ww}x{wh}"
                )
            return _text(f"Activated {app}")
        except Exception as e:
            return _error(f"activate_app failed: {e}")

    @tool(
        "get_active_window",
        "Get information about the currently active window.",
        {},
    )
    async def get_active_window_tool(args: dict[str, Any]) -> dict[str, Any]:
        try:
            win = platform.get_active_window()
            if win is None:
                return _text("No active window found")
            mapper.refresh()
            wx, wy, ww, wh = _window_position_api(win)
            return _text(json.dumps({
                "process_name": win.process_name,
                "window_title": win.window_title,
                "pid": win.pid,
                "x": wx,
                "y": wy,
                "width": ww,
                "height": wh,
            }, ensure_ascii=False))
        except Exception as e:
            return _error(f"get_active_window failed: {e}")

    # -- clipboard -----------------------------------------------------------

    @tool(
        "get_clipboard",
        "Read the current clipboard contents.",
        {},
    )
    async def get_clipboard_tool(args: dict[str, Any]) -> dict[str, Any]:
        try:
            clip = platform.get_clipboard()
            if clip.text:
                return _text(clip.text)
            if clip.files:
                return _text(f"Clipboard contains files: {clip.files}")
            return _text(f"Clipboard kind: {clip.kind} (no text content)")
        except Exception as e:
            return _error(f"get_clipboard failed: {e}")

    # -- menu ----------------------------------------------------------------

    @tool(
        "menu_click",
        'Click a menu item by path. Use " > " as separator, '
        'e.g. "File > Save As".',
        {
            "app": Annotated[str, "Application name"],
            "path": Annotated[str, 'Menu path, e.g. "File > Save"'],
        },
    )
    async def menu_click_tool(args: dict[str, Any]) -> dict[str, Any]:
        app = str(args["app"])
        path = str(args["path"])
        try:
            ok = platform.find_menu_item(app, path)
            if ok:
                return _text(f"Clicked menu: {path}")
            return _error(f"Menu item not found: {path}")
        except Exception as e:
            return _error(f"menu_click failed: {e}")

    @tool(
        "list_menu",
        "List available menu items for discovery. "
        "Pass empty path for top-level items.",
        {
            "app": Annotated[str, "Application name"],
            "path": Annotated[str, 'Partial menu path, e.g. "File" or "" for top-level'],
        },
    )
    async def list_menu_tool(args: dict[str, Any]) -> dict[str, Any]:
        app = str(args["app"])
        path = str(args.get("path", ""))
        try:
            items = platform.list_menu_items(app, path)
            if not items:
                return _text(f"No menu items found at '{path}' in {app}")
            return _text(", ".join(items))
        except Exception as e:
            return _error(f"list_menu failed: {e}")

    # -- assemble server -----------------------------------------------------

    all_tools = [
        screenshot_tool,
        left_click_tool,
        right_click_tool,
        double_click_tool,
        mouse_move_tool,
        type_text_tool,
        key_press_tool,
        scroll_tool,
        find_elements_tool,
        list_elements_tool,
        activate_app_tool,
        get_active_window_tool,
        get_clipboard_tool,
        menu_click_tool,
        list_menu_tool,
    ]

    return create_sdk_mcp_server(
        name="platform",
        version="0.1.0",
        tools=all_tools,
    )

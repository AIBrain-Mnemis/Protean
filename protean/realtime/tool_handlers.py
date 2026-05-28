"""Tool handlers — runs toolkit functions via Platform.

Shared execution logic used by both MCP server and TeachSession.
Each tool function takes a Platform + arguments dict, returns a result string.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

if TYPE_CHECKING:
    from collections.abc import Callable

    from protean.platform.base import Platform

log = logging.getLogger(__name__)

_ACTION_VIEW_WIDTH = 640
_ACTION_VIEW_HEIGHT = 480
_ACTION_VIEW_DIR = Path(tempfile.gettempdir()) / "protean_action_views"

# Screenshot compression settings (keeps images small for Claude Code context)
_SCREENSHOT_MAX_DIMENSION = 1024  # Resize if any side exceeds this
_SCREENSHOT_JPEG_QUALITY = 50     # JPEG quality (0-100), lower = smaller


def _compress_screenshot(png_path: Path, max_dim: int = _SCREENSHOT_MAX_DIMENSION,
                         quality: int = _SCREENSHOT_JPEG_QUALITY) -> Path:
    """Compress a PNG screenshot to JPEG with optional downscaling.

    Replaces the original file. Returns the new JPEG path.
    """
    jpg_path = png_path.with_suffix(".jpg")
    with Image.open(png_path) as img:
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            new_w = int(round(w * scale))
            new_h = int(round(h * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
        img = img.convert("RGB")  # JPEG doesn't support alpha
        img.save(jpg_path, "JPEG", quality=quality, optimize=True)
    # Remove original PNG
    png_path.unlink(missing_ok=True)
    log.debug(
        "Compressed screenshot: %s -> %s (%d bytes)",
        png_path.name, jpg_path.name, jpg_path.stat().st_size,
    )
    return jpg_path


def execute_tool(platform: Platform, name: str, args: dict[str, Any]) -> str:
    """Execute a toolkit function and return result string.

    Args:
        platform: Platform instance for GUI operations.
        name: Tool name (click, type_text, key_press, etc.)
        args: Tool arguments.

    Returns:
        Human-readable result string.
    """
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        raise ValueError(f"Unknown tool: {name}")
    return handler(platform, args)


# ── Tool handlers ────────────────────────────────────────


def _find_display_for_point(p: Platform, x: int, y: int):
    for display in p.get_displays():
        if (
            display.origin_x <= x < display.origin_x + display.width
            and display.origin_y <= y < display.origin_y + display.height
        ):
            return display
    return None


def _find_display_by_index(p: Platform, display_index: int):
    for display in p.get_displays():
        if display.display_index == display_index:
            return display
    return None


def _active_window_info(p: Platform):
    window = p.get_active_window()
    if window is None:
        return None, None
    cx = window.x + (window.width // 2)
    cy = window.y + (window.height // 2)
    return window, _find_display_for_point(p, cx, cy)


def _format_window_summary(p: Platform) -> str:
    window, display = _active_window_info(p)
    if window is None:
        return " No active window info available."

    title = window.window_title or "<untitled>"
    summary = (
        f" Active window: {window.process_name} title={title!r} "
        f"frame=(x={window.x}, y={window.y}, w={window.width}, h={window.height})"
    )
    if display is None:
        return summary

    return (
        f"{summary} display={display.display_index} "
        f"bounds=(x={display.origin_x}, y={display.origin_y}, "
        f"w={display.width}, h={display.height}, primary={display.is_primary})"
    )


def _capture_action_view(
    p: Platform,
    center_x: int,
    center_y: int,
    tool_name: str,
) -> dict[str, int | str] | None:
    display = _find_display_for_point(p, center_x, center_y)
    if display is None:
        return None

    width = min(_ACTION_VIEW_WIDTH, display.width)
    height = min(_ACTION_VIEW_HEIGHT, display.height)
    if width <= 0 or height <= 0:
        return None

    _ACTION_VIEW_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _ACTION_VIEW_DIR / f"_display_{uuid.uuid4().hex}.png"
    out_path = _ACTION_VIEW_DIR / (
        f"action_view_{time.strftime('%H%M%S')}_{tool_name}_{uuid.uuid4().hex[:8]}.jpg"
    )
    try:
        p.capture_display(display.display_index, tmp_path)

        with Image.open(tmp_path) as img:
            scale_x = img.width / display.width if display.width else 1.0
            scale_y = img.height / display.height if display.height else 1.0

            crop_w_px = max(1, int(round(width * scale_x)))
            crop_h_px = max(1, int(round(height * scale_y)))
            rel_x = center_x - display.origin_x
            rel_y = center_y - display.origin_y
            center_px_x = int(round(rel_x * scale_x))
            center_px_y = int(round(rel_y * scale_y))

            max_left = max(0, img.width - crop_w_px)
            max_top = max(0, img.height - crop_h_px)
            left_px = min(max(0, int(round(center_px_x - crop_w_px / 2))), max_left)
            top_px = min(max(0, int(round(center_px_y - crop_h_px / 2))), max_top)

            crop = img.crop((left_px, top_px, left_px + crop_w_px, top_px + crop_h_px))
            crop = crop.convert("RGB")
            crop.save(out_path, "JPEG", quality=_SCREENSHOT_JPEG_QUALITY, optimize=True)

        origin_x = display.origin_x + int(round(left_px / scale_x))
        origin_y = display.origin_y + int(round(top_px / scale_y))
        return {
            "path": str(out_path),
            "origin_x": origin_x,
            "origin_y": origin_y,
            "width": width,
            "height": height,
        }
    finally:
        tmp_path.unlink(missing_ok=True)


def _action_view_rect_for_mode(
    p: Platform,
    center_x: int,
    center_y: int,
    action_window,
    coordinate_mode: str,
    view: dict[str, int | str],
) -> tuple[str, str] | None:
    display = _find_display_for_point(p, center_x, center_y)
    if coordinate_mode == "display":
        if display is None:
            return None
        return (
            "display",
            f"(x={int(view['origin_x']) - display.origin_x}, "
            f"y={int(view['origin_y']) - display.origin_y}, "
            f"w={view['width']}, h={view['height']})",
        )

    if coordinate_mode == "window":
        if action_window is None:
            return None
        return (
            "window",
            f"(x={int(view['origin_x']) - action_window.x}, "
            f"y={int(view['origin_y']) - action_window.y}, "
            f"w={view['width']}, h={view['height']})",
        )

    if coordinate_mode == "global":
        return (
            "global",
            f"(x={view['origin_x']}, y={view['origin_y']}, "
            f"w={view['width']}, h={view['height']})",
        )

    raise ValueError(
        f"Unknown coordinate_mode: {coordinate_mode!r}. "
        f"Must be one of: global, display, window"
    )


def _append_action_view(
    result: str,
    p: Platform,
    center_x: int | None,
    center_y: int | None,
    action_window,
    tool_name: str,
    args: dict[str, Any],
) -> str:
    include_action_view = args.get("include_action_view", False)
    if not include_action_view or center_x is None or center_y is None:
        return result

    view = _capture_action_view(p, center_x, center_y, tool_name)
    if view is None:
        raise RuntimeError("failed to capture action view")

    default_coordinate_mode = "global" if tool_name in {"click_at", "move"} else "screen"
    coordinate_mode: str = args.get("coordinate_mode", default_coordinate_mode)
    rect = _action_view_rect_for_mode(
        p,
        center_x,
        center_y,
        action_window,
        coordinate_mode,
        view,
    )
    if rect is None:
        raise RuntimeError("failed to resolve action view rect")

    rect_mode, rect_value = rect

    return (
        f"{result} Action view: path={view['path']} "
        f"action_view_rect({rect_mode})={rect_value}"
    )


def _resolve_coordinates(
    p: Platform,
    args: dict[str, Any],
    tool_name: str,
) -> tuple[int, int, str] | str:
    px: int = args["x"]
    py: int = args["y"]
    coordinate_mode: str = args.get("coordinate_mode", "global")
    if coordinate_mode == "global":
        return px, py, "global"

    if coordinate_mode == "display":
        display_index: int = args.get("display", 0)
        display = _find_display_by_index(p, display_index)
        if display is None:
            return f"Display {display_index} not found"
        if not (0 <= px < display.width and 0 <= py < display.height):
            return (
                f"{tool_name} display-relative coordinates ({px}, {py}) are out of bounds "
                f"for display {display_index} ({display.width}x{display.height})"
            )
        return display.origin_x + px, display.origin_y + py, f"display {display_index}"

    if coordinate_mode == "window":
        window = p.get_active_window()
        if window is None:
            return f"{tool_name} with coordinate_mode='window' requires an active window"
        if not (0 <= px < window.width and 0 <= py < window.height):
            return (
                f"{tool_name} window-relative coordinates ({px}, {py}) are out of bounds "
                f"for active window ({window.width}x{window.height})"
            )
        return window.x + px, window.y + py, "active window"

    raise ValueError(
        f"{tool_name} coordinate_mode must be one of: global, display, window"
    )


def _click(p: Platform, args: dict[str, Any]) -> str:
    app: str = args["app"]
    label: str = args["label"]
    action_window = p.get_active_window()
    pos = p.find_element(app, label)
    if p.ax_press(app, label):
        return _append_action_view(
            f"Clicked '{label}' in {app} (via AXPress).{_format_window_summary(p)}",
            p,
            pos[0] if pos else None,
            pos[1] if pos else None,
            action_window,
            "click",
            args,
        )
    if pos is None:
        return f"Element '{label}' not found in {app}"
    p.click(pos[0], pos[1])
    return _append_action_view(
        f"Clicked '{label}' at ({pos[0]}, {pos[1]}) in {app}."
        f"{_format_window_summary(p)}",
        p,
        pos[0],
        pos[1],
        action_window,
        "click",
        args,
    )


def _click_at(p: Platform, args: dict[str, Any]) -> str:
    resolved = _resolve_coordinates(p, args, "click_at")
    if isinstance(resolved, str):
        return resolved
    px, py, origin = resolved
    action_window = p.get_active_window()
    p.click(px, py)
    return _append_action_view(
        f"Clicked at ({px}, {py}) using {origin} coordinates.{_format_window_summary(p)}",
        p,
        px,
        py,
        action_window,
        "click_at",
        args,
    )


def _move(p: Platform, args: dict[str, Any]) -> str:
    resolved = _resolve_coordinates(p, args, "move")
    if isinstance(resolved, str):
        return resolved
    px, py, origin = resolved
    action_window = p.get_active_window()
    p.move_cursor(px, py)
    return _append_action_view(
        f"Moved cursor to ({px}, {py}) using {origin} coordinates",
        p,
        px,
        py,
        action_window,
        "move",
        args,
    )


def _type_text(p: Platform, args: dict[str, Any]) -> str:
    app: str = args.get("app", "")
    label: str = args.get("label", "")
    text: str = args["text"]
    if app and label:
        pos = p.find_element(app, label)
        if pos is None:
            return f"Input field '{label}' not found in {app}"
        p.click(pos[0], pos[1])
    p.type_text(text)
    return f"Typed '{text}'"


def _key_press(p: Platform, args: dict[str, Any]) -> str:
    keys: str = args["keys"]
    key_list = [k.strip() for k in keys.split("+")]
    p.key_press(*key_list)
    return f"Pressed {keys}"


def _activate_app(p: Platform, args: dict[str, Any]) -> str:
    app: str = args["app"]
    p.activate_app(app)
    time.sleep(0.3)
    return f"Activated {app}.{_format_window_summary(p)}"


def _menu_click(p: Platform, args: dict[str, Any]) -> str:
    app: str = args["app"]
    path: str = args["path"]
    if p.find_menu_item(app, path):
        return f"Clicked menu '{path}' in {app}"
    return f"Menu item '{path}' not found in {app}"


def _select_option(p: Platform, args: dict[str, Any]) -> str:
    app: str = args["app"]
    label: str = args["label"]
    value: str = args["value"]
    role = p.get_element_role(app, label)
    if role is None:
        return (
            f"Dropdown '{label}' not found in {app}. "
            f"Use list_elements to discover available dropdowns."
        )
    if role == "AXComboBox":
        return (
            f"'{label}' is a ComboBox (text input with suggestions), not a dropdown. "
            f"Use type_text(app='{app}', label='{label}', text='{value}') instead."
        )
    if p.select_option(app, label, value):
        return f"Selected '{value}' from '{label}' in {app}"
    return f"Failed to select '{value}' from '{label}' in {app}"


def _find_elements(p: Platform, args: dict[str, Any]) -> str:
    app: str = args["app"]
    query: str = args["query"]
    results = p.find_elements(app, query)
    if not results:
        return f"No elements matching '{query}' found in {app}"
    lines = []
    for i, el in enumerate(results, 1):
        label = el.label if len(el.label) <= 80 else el.label[:77] + "..."
        lines.append(
            f"  {i}. {el.role}: {label!r}"
            f" center=({el.center_x},{el.center_y})"
            f" size={el.width}x{el.height}"
        )
    return f"{len(results)} element(s) matching '{query}' in {app}:\n" + "\n".join(lines)


_SCREENSHOT_PREFIX = "Screenshot saved to "
_SCREENSHOT_SUFFIX = ". Use the Read tool to view it."
_SCREENSHOT_PATH_RE = re.compile(
    re.escape(_SCREENSHOT_PREFIX) + r"(\S+?)" + re.escape(_SCREENSHOT_SUFFIX)
)


def format_screenshot_result(path: str | Path) -> str:
    return f"{_SCREENSHOT_PREFIX}{path}{_SCREENSHOT_SUFFIX}"


def extract_screenshot_paths(text: str) -> list[str]:
    """Extract screenshot paths from tool result text."""
    return _SCREENSHOT_PATH_RE.findall(text)


def _screenshot(p: Platform, args: dict[str, Any]) -> str:
    display_index: int = args.get("display", 0)
    if display_index == 0:
        _, display = _active_window_info(p)
        if display is not None:
            display_index = display.display_index
        else:
            cx, cy = p.get_cursor_position()
            d = _find_display_for_point(p, cx, cy)
            display_index = d.display_index if d else 1
    ts = time.strftime("%H%M%S")
    path = Path(tempfile.gettempdir()) / f"protean_screenshot_{ts}.png"
    p.capture_display(display_index, path)
    path = _compress_screenshot(path)
    return format_screenshot_result(path)


_TOOL_HANDLERS: dict[str, Callable[[Platform, dict[str, Any]], str]] = {
    "click": _click,
    "click_at": _click_at,
    "move": _move,
    "type_text": _type_text,
    "key_press": _key_press,
    "activate_app": _activate_app,
    "menu_click": _menu_click,
    "select_option": _select_option,
    "screenshot": _screenshot,
    "find_elements": _find_elements,
}

"""Provider-neutral GUI action execution.

Both ``providers/computer_use`` (Anthropic Computer Use API) and the MCP
stdio server in ``protean.mcp`` need the same primitives — take a
screenshot, click at (x, y), type text, key_press, scroll, etc. — and
both want the same post-action behavior: append a fresh screenshot so
the model doesn't have to make a separate screenshot call after every
action.

This module owns that shared behavior. ``ActionExecutor`` wraps a
``Platform`` + ``CoordinateMapper`` and exposes one async method per
GUI action, each returning a provider-neutral ``ActionResult``. Each
provider translates ``ActionResult`` into its own wire format
(Anthropic content blocks vs. MCP ``content`` array).

Action methods deliberately do NOT catch ``Platform`` exceptions — they
propagate so callers can format errors however their host API expects
(``ComputerUseExecutor._format_tool_error`` for Anthropic / OpenAI,
``"<Action> failed: {e}"`` for MCP).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFont

from protean.platform.base import (
    LLM_JPEG_QUALITY,
    parse_key_combo,
    prepare_screenshot_for_llm,
)

if TYPE_CHECKING:
    from protean.platform.base import CoordinateMapper, Platform

log = logging.getLogger(__name__)

# Pixel spacing of the coordinate grid drawn on the detail-crop view.
DETAIL_GRID_STEP = 100


@dataclass
class ActionResult:
    """Provider-neutral outcome of one GUI action.

    Pure data — wire-format translation lives in each consumer
    (``computer_use._result_to_anthropic`` for the Anthropic API,
    direct field access in the OpenAI loop, ``mcp.server.result_to_mcp``
    for the MCP surface). Keeping ``ActionResult`` ignorant of provider
    formats is what makes the same executor primitive reusable.

    - ``text``: status line (always set on success; None for plain screenshots).
    - ``screenshot_b64``: post-action full screenshot, JPEG base64. None
      when the action was called with ``include_screenshot=False``.
    - ``detail_crop_b64`` + ``detail_caption``: 2x-zoomed crop with a
      coordinate-grid overlay around (ix, iy), for coordinate actions
      only. Helps the model verify or correct click placement.
    """

    text: str | None = None
    screenshot_b64: str | None = None
    detail_crop_b64: str | None = None
    detail_caption: str | None = None


class ActionExecutor:
    """Execute GUI actions against a Platform, return neutral results.

    Single source of truth for the "post-action screenshot" contract:
    every action with ``include_screenshot=True`` (the default) bundles a
    fresh screenshot. Coordinate actions additionally bundle a
    detail-crop with a coordinate-grid overlay.

    Errors propagate. Callers wrap them with their own error format.
    """

    def __init__(self, platform: Platform, mapper: CoordinateMapper) -> None:
        self._platform = platform
        self._mapper = mapper
        # Last full screenshot at API resolution. ``detail_crop`` reads
        # it so it doesn't re-capture.
        self._last_img: Image.Image | None = None

    # ─── core capture ──────────────────────────────────────────────────

    def take_screenshot(self) -> str:
        """Capture the active display, return JPEG base64 at API resolution.

        Refreshes the coordinate mapper first so that the captured display
        matches the scale used by subsequent ``to_actual()`` calls. Caches
        the resized PIL image on the instance so ``detail_crop()`` can
        zoom without re-capturing.
        """
        self._mapper.refresh()
        scale = self._mapper.scale
        tmp = Path(tempfile.gettempdir()) / f"protean_actions_{time.monotonic_ns()}.png"
        try:
            self._platform.capture_display(self._mapper.display_index, tmp)
            raw_bytes = tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)
        jpeg_bytes, _ = prepare_screenshot_for_llm(
            raw_bytes,
            max_width=scale.api_w,
            max_height=scale.api_h,
            quality=LLM_JPEG_QUALITY,
            exact_size=True,
        )
        self._last_img = Image.open(io.BytesIO(jpeg_bytes)).copy()
        return base64.b64encode(jpeg_bytes).decode("ascii")

    def screenshot_result(self, text: str | None = None) -> ActionResult:
        screenshot_b64 = self.take_screenshot()
        context = self._screenshot_context_text()
        result_text = context if text is None else f"{text}. {context}"
        return ActionResult(text=result_text, screenshot_b64=screenshot_b64)

    def detail_crop(self, cx: int, cy: int) -> str | None:
        """Return base64 JPEG of a 2x zoom around (cx, cy) with a coord grid.

        Returns None when there's no cached screenshot yet (call
        ``take_screenshot()`` first).

        Crop side = ``max(display_width, display_height) / 4``, centered
        on (cx, cy). Grid step = ``DETAIL_GRID_STEP``.
        """
        if self._last_img is None:
            return None

        img = self._last_img
        step = DETAIL_GRID_STEP
        full_w, full_h = img.size
        crop_r = max(1, max(full_w, full_h) // 8)
        x1, y1 = max(0, cx - crop_r), max(0, cy - crop_r)
        x2, y2 = min(full_w, cx + crop_r), min(full_h, cy + crop_r)
        crop = img.crop((x1, y1, x2, y2))

        crop_w, crop_h = crop.size
        crop = crop.resize((crop_w * 2, crop_h * 2), Image.LANCZOS)  # type: ignore[attr-defined]
        draw = ImageDraw.Draw(crop)
        font = ImageFont.load_default()

        # Vertical grid lines with x-coordinate labels.
        for gx in range((x1 // step) * step, x2 + 1, step):
            sx = (gx - x1) * 2
            if 0 <= sx <= crop_w * 2:
                draw.line([(sx, 0), (sx, crop_h * 2)], fill="red", width=1)
                txt = str(gx)
                bbox = font.getbbox(txt)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                draw.rectangle([(sx + 1, 0), (sx + 1 + tw + 4, th + 4)], fill="white")
                draw.text((sx + 3, 1), txt, fill="black", font=font)

        # Horizontal grid lines with y-coordinate labels.
        for gy in range((y1 // step) * step, y2 + 1, step):
            sy = (gy - y1) * 2
            if 0 <= sy <= crop_h * 2:
                draw.line([(0, sy), (crop_w * 2, sy)], fill="red", width=1)
                txt = str(gy)
                bbox = font.getbbox(txt)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                draw.rectangle([(0, sy + 1), (tw + 4, sy + 1 + th + 4)], fill="white")
                draw.text((2, sy + 2), txt, fill="black", font=font)

        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=LLM_JPEG_QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # ─── actions: mouse ────────────────────────────────────────────────

    async def left_click(
        self, ix: int, iy: int, *, include_screenshot: bool = True,
    ) -> ActionResult:
        x, y = self._mapper.to_actual(ix, iy)
        self._platform.click(x, y)
        await asyncio.sleep(0.5)
        return self._coord_result(f"Clicked at ({ix}, {iy})", ix, iy, include_screenshot)

    async def right_click(
        self, ix: int, iy: int, *, include_screenshot: bool = True,
    ) -> ActionResult:
        x, y = self._mapper.to_actual(ix, iy)
        self._platform.click(x, y, button="right")
        await asyncio.sleep(0.5)
        return self._coord_result(f"Right-clicked at ({ix}, {iy})", ix, iy, include_screenshot)

    async def double_click(
        self, ix: int, iy: int, *, include_screenshot: bool = True,
    ) -> ActionResult:
        x, y = self._mapper.to_actual(ix, iy)
        self._platform.double_click(x, y)
        await asyncio.sleep(0.5)
        return self._coord_result(f"Double-clicked at ({ix}, {iy})", ix, iy, include_screenshot)

    async def mouse_move(
        self, ix: int, iy: int, *, include_screenshot: bool = True,
    ) -> ActionResult:
        x, y = self._mapper.to_actual(ix, iy)
        self._platform.move_cursor(x, y)
        await asyncio.sleep(0.2)
        return self._coord_result(f"Moved cursor to ({ix}, {iy})", ix, iy, include_screenshot)

    # ─── actions: keyboard ─────────────────────────────────────────────

    async def type_text(
        self, text: str, *, include_screenshot: bool = True,
    ) -> ActionResult:
        self._platform.type_text(text)
        await asyncio.sleep(0.5)
        msg = f"Typed: {text!r}"
        if not include_screenshot:
            return ActionResult(text=msg)
        return self.screenshot_result(msg)

    async def key_press(
        self, keys_str: str, *, include_screenshot: bool = True,
    ) -> ActionResult:
        keys = parse_key_combo(keys_str)
        self._platform.key_press(*keys)
        await asyncio.sleep(0.5)
        msg = f"Pressed: {keys_str}"
        if not include_screenshot:
            return ActionResult(text=msg)
        return self.screenshot_result(msg)

    # ─── actions: scroll ───────────────────────────────────────────────

    async def scroll(
        self,
        ix: int,
        iy: int,
        direction: str,
        amount: int,
        *,
        include_screenshot: bool = True,
    ) -> ActionResult:
        x, y = self._mapper.to_actual(ix, iy)
        self._platform.scroll(x, y, direction, amount)
        await asyncio.sleep(0.3)
        return self._coord_result(
            f"Scrolled {direction} {amount} steps at ({ix}, {iy})",
            ix,
            iy,
            include_screenshot,
        )

    # ─── screenshot ────────────────────────────────────────────────────

    def screenshot(self) -> ActionResult:
        """Take a screenshot as its own action (no text confirmation)."""
        return self.screenshot_result()

    # ─── actions: app / window / clipboard ─────────────────────────────

    async def activate_app(
        self, app: str, *, include_screenshot: bool = True,
    ) -> ActionResult:
        self._platform.activate_app(app)
        await asyncio.sleep(0.5)
        win = self._platform.get_active_window()
        if win is not None:
            self._mapper.refresh()
            wx, wy, ww, wh = self._window_position_api(win)
            msg = (
                f'Activated {app}. Active window: "{win.window_title}" '
                f"({win.process_name}) at ({wx}, {wy}) size {ww}x{wh}"
            )
        else:
            msg = f"Activated {app}"
        if not include_screenshot:
            return ActionResult(text=msg)
        return self.screenshot_result(msg)

    def get_active_window(self) -> ActionResult:
        win = self._platform.get_active_window()
        if win is None:
            return ActionResult(text="No active window found")
        self._mapper.refresh()
        wx, wy, ww, wh = self._window_position_api(win)
        return ActionResult(text=json.dumps(
            {
                "process_name": win.process_name,
                "window_title": win.window_title,
                "pid": win.pid,
                "x": wx,
                "y": wy,
                "width": ww,
                "height": wh,
            },
            ensure_ascii=False,
        ))

    def get_clipboard(self) -> ActionResult:
        clip = self._platform.get_clipboard()
        if clip.text:
            return ActionResult(text=clip.text)
        if clip.files:
            return ActionResult(text=f"Clipboard contains files: {clip.files}")
        return ActionResult(text=f"Clipboard kind: {clip.kind} (no text content)")

    # ─── actions: timing ──────────────────────────────────────────

    async def wait(
        self, seconds: float = 2, *, include_screenshot: bool = True,
    ) -> ActionResult:
        """Pause the agentic loop. Useful for waiting on slow UI transitions
        (app launch, page load) that finish later than our default settle."""
        await asyncio.sleep(seconds)
        msg = f"Waited {seconds}s"
        if not include_screenshot:
            return ActionResult(text=msg)
        return self.screenshot_result(msg)

    # ─── internals ────────────────────────────────────────────────────

    def _screenshot_context_text(self) -> str:
        if self._last_img is None:
            raise RuntimeError("screenshot context requested before screenshot capture")
        width, height = self._last_img.size
        return (
            f"Screenshot: display {self._mapper.display_index}, size {width}x{height}, "
            f"x=0..{width - 1}, y=0..{height - 1}."
        )

    def _coord_result(
        self, base_text: str, ix: int, iy: int, include_screenshot: bool,
    ) -> ActionResult:
        """Wrap a coordinate-based action result: text + screenshot + detail.

        Post-action screenshot capture failures propagate — matches the
        original computer_use behavior where a failed screenshot turns
        the whole tool result into an error.
        """
        if not include_screenshot:
            return ActionResult(text=base_text)
        ss = self.take_screenshot()
        context = self._screenshot_context_text()
        crop = self.detail_crop(ix, iy)
        caption: str | None = None
        if crop is not None:
            if self._last_img is None:
                raise RuntimeError("detail caption requested before screenshot capture")
            w, h = self._last_img.size
            crop_r = max(1, max(w, h) // 8)
            x1, y1 = max(0, ix - crop_r), max(0, iy - crop_r)
            x2, y2 = min(w, ix + crop_r), min(h, iy + crop_r)
            caption = (
                f"[Detail view around ({ix}, {iy}) — "
                f"region x={x1}..{x2}, y={y1}..{y2}, with coordinate grid overlay]"
            )
        return ActionResult(
            text=f"{base_text}. {context}",
            screenshot_b64=ss,
            detail_crop_b64=crop,
            detail_caption=caption,
        )

    def _window_position_api(self, win) -> tuple[int, int, int, int]:
        """Translate a Window's real pixel rect into API coordinate space.

        Origin goes through ``to_api`` (subtract display origin, scale).
        Width/height are sizes, not points, so they only get scaled.
        """
        x_api, y_api = self._mapper.to_api(win.x, win.y)
        scale = self._mapper.scale
        w_api = int(round(win.width * scale.api_w / scale.actual_w))
        h_api = int(round(win.height * scale.api_h / scale.actual_h))
        return x_api, y_api, w_api, h_api

    # ─── dispatch ────────────────────────────────────────────────────

    async def dispatch(
        self,
        name: str,
        args: dict[str, Any],
        *,
        include_screenshot: bool = True,
    ) -> ActionResult:
        """Run the GUI tool named ``name`` with ``args``.

        Single source of truth for the wire-name → method mapping. Used
        by ``ComputerUseExecutor._execute_tool`` and the MCP server in
        ``protean.mcp.server`` so neither has to keep its own switch.

        Argument coercion (``_safe_int`` for coordinates, ``str`` for
        text fields) happens here so the spec stays the one place that
        defines the schema and the one place that decodes it.

        Raises if ``name`` is unknown or if the underlying ``Platform``
        call fails; callers wrap with their own error-format conventions.
        """
        if name == "screenshot":
            return self.screenshot()
        if name == "left_click":
            return await self.left_click(
                _safe_int(args["x"]), _safe_int(args["y"]),
                include_screenshot=include_screenshot,
            )
        if name == "right_click":
            return await self.right_click(
                _safe_int(args["x"]), _safe_int(args["y"]),
                include_screenshot=include_screenshot,
            )
        if name == "double_click":
            return await self.double_click(
                _safe_int(args["x"]), _safe_int(args["y"]),
                include_screenshot=include_screenshot,
            )
        if name == "mouse_move":
            return await self.mouse_move(
                _safe_int(args["x"]), _safe_int(args["y"]),
                include_screenshot=include_screenshot,
            )
        if name == "type_text":
            return await self.type_text(
                str(args["text"]), include_screenshot=include_screenshot,
            )
        if name == "key_press":
            return await self.key_press(
                str(args["keys"]), include_screenshot=include_screenshot,
            )
        if name == "scroll":
            return await self.scroll(
                _safe_int(args["x"]),
                _safe_int(args["y"]),
                args["direction"],
                args.get("amount", 3),
                include_screenshot=include_screenshot,
            )
        if name == "wait":
            return await self.wait(
                float(args.get("seconds", 2)),
                include_screenshot=include_screenshot,
            )
        if name == "activate_app":
            return await self.activate_app(
                str(args["app"]), include_screenshot=include_screenshot,
            )
        if name == "get_active_window":
            return self.get_active_window()
        if name == "get_clipboard":
            return self.get_clipboard()
        raise ValueError(f"Unknown action: {name}")


def _safe_int(value: Any) -> int:
    """Coerce a JSON-decoded coordinate to int.

    Models sometimes emit ``"955, 332"`` for an ``x`` field when they
    mean two separate values; take the first number rather than blow
    up on int(). Used by ``dispatch`` so every coordinate-taking action
    is forgiving in the same way.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    s = str(value).strip()
    if "," in s:
        s = s.split(",")[0].strip()
    return int(s)


@dataclass(frozen=True)
class ToolSpec:
    """Wire descriptor for a GUI tool exposed to LLM agents.

    One spec drives both the Anthropic / OpenAI function-tool list in
    ``ComputerUseExecutor`` (via ``to_function_tool()``) and the MCP
    server in ``protean.mcp.server`` (via the ``mcp`` Annotated schema
    fields). Adding a new GUI action: implement the method on
    ``ActionExecutor``, add a branch in ``ActionExecutor.dispatch``, add
    a spec here. Consumers pick it up automatically.
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_function_tool(self) -> dict[str, Any]:
        """Return the Anthropic / OpenAI function-tool descriptor."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _coord_schema(x_desc: str, y_desc: str) -> dict[str, Any]:
    """Helper: JSON schema for an (x, y) coordinate pair."""
    return {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": x_desc},
            "y": {"type": "integer", "description": y_desc},
        },
        "required": ["x", "y"],
    }


GUI_TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="screenshot",
        description=(
            "Take a screenshot of the current screen. Returns the image. "
            "Call this first to see what's on screen before acting."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        name="left_click",
        description=(
            "Click the left mouse button at the given (x, y) pixel coordinates. "
            "Coordinates are relative to the screenshot image. The image is "
            "1024 px wide and preserves the active display's aspect ratio; "
            "each screenshot result states its exact size and valid ranges."
        ),
        input_schema=_coord_schema(
            "X coordinate (0-1023)",
            "Y coordinate (0 to screenshot height - 1)",
        ),
    ),
    ToolSpec(
        name="right_click",
        description="Right-click at the given (x, y) pixel coordinates.",
        input_schema=_coord_schema("X coordinate", "Y coordinate"),
    ),
    ToolSpec(
        name="double_click",
        description="Double-click the left mouse button at (x, y).",
        input_schema=_coord_schema("X coordinate", "Y coordinate"),
    ),
    ToolSpec(
        name="mouse_move",
        description="Move the mouse cursor to (x, y) without clicking.",
        input_schema=_coord_schema("X coordinate", "Y coordinate"),
    ),
    ToolSpec(
        name="type_text",
        description=(
            "Type the given text string. The text is typed character by character "
            "into whatever field currently has focus. Use left_click first to focus "
            "the target input field."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["text"],
        },
    ),
    ToolSpec(
        name="key_press",
        description=(
            "Press a key or key combination. Examples: 'return', 'tab', 'escape', "
            "'cmd+c', 'cmd+v', 'cmd+a', 'ctrl+c', 'alt+tab', 'shift+tab', "
            "'cmd+shift+n', 'up', 'down', 'left', 'right', 'backspace', 'delete', "
            "'space', 'cmd+w', 'cmd+q'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "Key or combo, e.g. 'return', 'cmd+c', 'alt+tab'",
                },
            },
            "required": ["keys"],
        },
    ),
    ToolSpec(
        name="scroll",
        description=(
            "Scroll at the given (x, y) position. Direction can be 'up', 'down', "
            "'left', or 'right'. Amount is the number of scroll steps (default 3)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate to scroll at"},
                "y": {"type": "integer", "description": "Y coordinate to scroll at"},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "description": "Scroll direction",
                },
                "amount": {
                    "type": "integer",
                    "description": "Number of scroll steps (default 3)",
                },
            },
            "required": ["x", "y", "direction"],
        },
    ),
    ToolSpec(
        name="wait",
        description="Wait for the given number of seconds (e.g. for loading).",
        input_schema={
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Seconds to wait (default 2)",
                },
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="activate_app",
        description=(
            "Bring the given application to the foreground. Returns text "
            "describing the new active window plus a post-action screenshot. "
            "More reliable than the keyboard launcher when you know the app name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Application name, process name, or bundle ID",
                },
            },
            "required": ["app"],
        },
    ),
    ToolSpec(
        name="get_active_window",
        description=(
            "Get the currently active window as JSON (process_name, "
            "window_title, pid, x, y, width, height). Cheap focus check "
            "that doesn't burn a screenshot."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        name="get_clipboard",
        description=(
            "Read the current clipboard contents. Use this to check what "
            "was just copied (e.g. after Cmd+C) without screenshotting."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
]


GUI_TOOL_NAMES: frozenset[str] = frozenset(spec.name for spec in GUI_TOOL_SPECS)
"""Names of all tools dispatched via ``ActionExecutor.dispatch``.

Consumers use this to decide which incoming tool calls go to the shared
``dispatch`` versus their own provider-specific handlers (e.g.
computer_use's ``done`` / terminal tools).
"""

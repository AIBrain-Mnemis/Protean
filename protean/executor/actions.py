"""Provider-neutral GUI action execution.

Both ``providers/computer_use`` (Anthropic Computer Use API) and the MCP
stdio server in ``protean.mcp`` need the same primitives — take a
screenshot, click at (x, y), drag, type text, key_press, scroll, etc. — and
both want the same post-action behavior: append a fresh screenshot so
the model doesn't have to make a separate screenshot call after every
action.

This module owns that shared behavior. ``ActionExecutor`` wraps a
``Platform`` + ``CoordinateMapper`` and exposes one async method per
GUI action, each returning a provider-neutral ``ActionResult``. Each
provider translates ``ActionResult`` into its own wire format
(Anthropic content blocks vs. MCP ``content`` array).

Action methods deliberately do NOT catch ``Platform`` exceptions — they
propagate so callers can wrap them with ``format_tool_error()`` (below)
into their own wire format (Anthropic/OpenAI tool_result text vs. MCP
error content).
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
from typing import TYPE_CHECKING, Any, cast, get_args

from PIL import Image, ImageDraw, ImageFont

from protean.platform.base import (
    LLM_JPEG_QUALITY,
    MouseButton,
    Rect,
    ScrollDirection,
    parse_key_combo,
    prepare_screenshot_for_llm,
    window_matches_app,
)

if TYPE_CHECKING:
    from protean.platform.base import AccessibilityNode, CoordinateMapper, Platform

log = logging.getLogger(__name__)

# Pixel spacing of the coordinate grid drawn on the detail-crop view.
DETAIL_GRID_STEP = 100
A11Y_NODE_BUDGET = 80
A11Y_QUERY_NODE_BUDGET = 24
A11Y_TIMEOUT_SEC = 0.5
A11Y_VISIT_BUDGET = 600

_VALID_MOUSE_BUTTONS = get_args(MouseButton)
_VALID_SCROLL_DIRECTIONS = get_args(ScrollDirection)


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
        """Capture the selected display, return JPEG base64 at API resolution.

        The coordinate mapper owns display selection. This method faithfully
        captures that display without inspecting focus or changing coordinate
        context. The resized image is cached for ``detail_crop()``.
        """
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

    def screenshot_result(
        self,
        text: str | None = None,
        *,
        query: str = "",
        app: str = "",
    ) -> ActionResult:
        screenshot_b64 = self.take_screenshot()
        context = self._observation_context_text(query, app=app)
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

    async def click(
        self,
        ix: int,
        iy: int,
        *,
        button: MouseButton = "left",
        click_count: int = 1,
        include_screenshot: bool = True,
    ) -> ActionResult:
        if button not in _VALID_MOUSE_BUTTONS:
            raise ValueError(
                f"Invalid button {button!r}; expected one of {_VALID_MOUSE_BUTTONS}"
            )
        x, y = self._mapper.to_actual(ix, iy)
        self._platform.click(x, y, button=button, click_count=click_count)
        await asyncio.sleep(0.5)
        verb = {"left": "Clicked", "right": "Right-clicked", "middle": "Middle-clicked"}.get(
            button, "Clicked",
        )
        if click_count >= 2:
            verb = f"{verb} {click_count}x"
        return self._coord_result(f"{verb} at ({ix}, {iy})", ix, iy, include_screenshot)

    async def mouse_move(
        self, ix: int, iy: int, *, include_screenshot: bool = True,
    ) -> ActionResult:
        x, y = self._mapper.to_actual(ix, iy)
        self._platform.move_cursor(x, y)
        await asyncio.sleep(0.2)
        return self._coord_result(f"Moved cursor to ({ix}, {iy})", ix, iy, include_screenshot)

    async def drag(
        self,
        from_ix: int,
        from_iy: int,
        to_ix: int,
        to_iy: int,
        *,
        from_display: int | None = None,
        to_display: int | None = None,
        include_screenshot: bool = True,
    ) -> ActionResult:
        current_display = self._mapper.display_index
        source = self._display(from_display) if from_display is not None else None
        target = self._display(to_display) if to_display is not None else None
        from_x, from_y = (
            self._mapper.to_actual_on(source, from_ix, from_iy)
            if source is not None
            else self._mapper.to_actual(from_ix, from_iy)
        )
        to_x, to_y = (
            self._mapper.to_actual_on(target, to_ix, to_iy)
            if target is not None
            else self._mapper.to_actual(to_ix, to_iy)
        )
        self._platform.drag(from_x, from_y, to_x, to_y)
        if target is not None:
            self._mapper.select_display(target)
        await asyncio.sleep(0.5)
        source_index = source.display_index if source is not None else current_display
        target_index = target.display_index if target is not None else current_display
        msg = (
            f"Dragged from display {source_index} ({from_ix}, {from_iy}) "
            f"to display {target_index} ({to_ix}, {to_iy})"
        )
        return self._coord_result(msg, to_ix, to_iy, include_screenshot)

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
        direction: ScrollDirection,
        amount: int,
        *,
        include_screenshot: bool = True,
    ) -> ActionResult:
        if direction not in _VALID_SCROLL_DIRECTIONS:
            raise ValueError(f"Invalid scroll direction: {direction!r}")
        if amount < 1:
            raise ValueError(f"Scroll amount must be positive, got {amount}")
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

    def screenshot(self, *, query: str = "", display: int | None = None) -> ActionResult:
        """Take a screenshot, optionally switching to a specific display."""
        if display is not None:
            self._mapper.select_display(self._display(display))
        return self.screenshot_result(query=query)

    # ─── actions: app / window / clipboard ─────────────────────────────

    async def activate_app(
        self, app: str, *, include_screenshot: bool = True,
    ) -> ActionResult:
        activated = self._platform.activate_app(app)
        await asyncio.sleep(0.5)
        win = self._platform.get_active_window()
        if win is None or win.pid != activated.pid:
            raise RuntimeError(
                f"Application {app!r} lost focus after activation; active window is {win}"
            )
        selected = self._display_for_window(win)
        if selected is None:
            raise RuntimeError(f"Active window is outside all displays: {win}")
        self._mapper.select_display(selected)
        wx, wy, ww, wh = self._window_position_api(win)
        msg = (
            f'Activated {app}. Active window: "{win.window_title}" '
            f"({win.process_name}) at ({wx}, {wy}) size {ww}x{wh}"
        )
        if not include_screenshot:
            return ActionResult(text=msg)
        return self.screenshot_result(msg, app=app)

    def list_windows(self, *, app: str = "") -> ActionResult:
        """List visible windows that the platform can activate by ID."""
        app_key = app.strip().casefold()
        active = self._platform.get_active_window()
        windows: list[dict[str, Any]] = []
        for window in self._platform.list_windows():
            if app_key and not window_matches_app(window, app_key):
                continue
            display = self._display_for_window(window)
            windows.append({
                "window_id": window.window_id,
                "process_name": window.process_name,
                "window_title": window.window_title,
                "pid": window.pid,
                "display": display.display_index if display is not None else None,
                "global_x": window.x,
                "global_y": window.y,
                "width": window.width,
                "height": window.height,
                "active": bool(
                    active is not None
                    and active.window_id
                    and active.window_id == window.window_id
                ),
            })
        return ActionResult(text=json.dumps(windows, ensure_ascii=False))

    async def activate_window(
        self, window_id: str, *, include_screenshot: bool = True,
    ) -> ActionResult:
        activated = self._platform.activate_window(window_id)
        await asyncio.sleep(0.5)
        win = self._platform.get_active_window()
        if win is None or win.window_id != activated.window_id:
            raise RuntimeError(
                f"Window {window_id!r} lost focus after activation; active window is {win}"
            )
        selected = self._display_for_window(win)
        if selected is None:
            raise RuntimeError(f"Active window is outside all displays: {win}")
        self._mapper.select_display(selected)
        wx, wy, ww, wh = self._window_position_api(win)
        msg = (
            f'Activated window {win.window_id}. Active window: "{win.window_title}" '
            f"({win.process_name}) on display {selected.display_index} "
            f"at ({wx}, {wy}) size {ww}x{wh}"
        )
        if not include_screenshot:
            return ActionResult(text=msg)
        return self.screenshot_result(msg, app=win.process_name)

    def get_active_window(self) -> ActionResult:
        win = self._platform.get_active_window()
        if win is None:
            return ActionResult(text="No active window found")
        display = self._display_for_window(win)
        return ActionResult(text=json.dumps(
            {
                "window_id": win.window_id,
                "process_name": win.process_name,
                "window_title": win.window_title,
                "pid": win.pid,
                "display": display.display_index if display is not None else None,
                "global_x": win.x,
                "global_y": win.y,
                "width": win.width,
                "height": win.height,
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

    def _display(self, display_index: int):
        display = next(
            (
                item
                for item in self._platform.get_displays()
                if item.display_index == display_index
            ),
            None,
        )
        if display is None:
            raise ValueError(f"Display {display_index} is not available")
        return display

    def _screenshot_context_text(self) -> str:
        if self._last_img is None:
            raise RuntimeError("screenshot context requested before screenshot capture")
        width, height = self._last_img.size
        displays = self._platform.get_displays()
        context = (
            f"Screenshot: display {self._mapper.display_index} of {len(displays)}, "
            f"size {width}x{height}, "
            f"x=0..{width - 1}, y=0..{height - 1}."
        )
        active = self._platform.get_active_window()
        if (
            active is not None
            and active.window_id
            and self._window_intersects_selected_display(active)
        ):
            context += (
                f" Focused window: id={active.window_id!r}, "
                f"app={active.process_name!r}, title={active.window_title!r}."
            )
        return context

    def _observation_context_text(self, query: str = "", *, app: str = "") -> str:
        context = self._screenshot_context_text()
        a11y_context = self._accessibility_context_text(query, app=app)
        if a11y_context:
            return f"{context}\n{a11y_context}"
        return context

    def _accessibility_context_text(self, query: str = "", *, app: str = "") -> str:
        if not app:
            active = self._platform.get_active_window()
            if active is not None and not self._window_intersects_selected_display(active):
                return (
                    "Accessibility context unavailable: focused window is outside "
                    f"display {self._mapper.display_index}."
                )
        budget = A11Y_QUERY_NODE_BUDGET if query.strip() else A11Y_NODE_BUDGET
        scale = self._mapper.scale
        snapshot = self._platform.accessibility_snapshot(
            query.strip(),
            app=app,
            visible_bounds=Rect(scale.origin_x, scale.origin_y, scale.actual_w, scale.actual_h),
            max_nodes=budget,
            max_visited=A11Y_VISIT_BUDGET,
            timeout=A11Y_TIMEOUT_SEC,
        )
        if snapshot.unavailable_reason:
            return f"Accessibility context unavailable: {snapshot.unavailable_reason}."

        prefix = "Accessibility matches" if query.strip() else "Accessibility context"
        title = snapshot.window_title or "<untitled>"
        lines = [
            f"{prefix}: controls visible in the current screenshot; "
            f"active window {snapshot.app!r} title={title!r}"
        ]
        formatted = [
            line
            for node in sorted(snapshot.nodes, key=self._a11y_node_rank)
            if (line := self._format_a11y_node(node))
        ]
        if not formatted:
            if query.strip():
                lines.append(f"  no matching controls for {query!r}")
            else:
                lines.append("  no visible labeled controls found")
        else:
            lines.extend(formatted)
        if snapshot.truncated:
            lines.append("  [truncated]")
        return "\n".join(lines)

    def _format_a11y_node(self, node: "AccessibilityNode") -> str:
        visible_rect = self._mapper.scale.visible_api_rect(
            node.x,
            node.y,
            node.width,
            node.height,
        )
        if visible_rect is None:
            return ""
        rect_x, rect_y, rect_w, rect_h = visible_rect
        cx = rect_x + rect_w // 2
        cy = rect_y + rect_h // 2

        label = node.label or node.value or node.description or "<unlabeled>"
        label = label.replace("\n", " ")[:80]
        suffix_parts = [*node.states, *node.actions]
        suffix = f" {' '.join(suffix_parts)}" if suffix_parts else ""
        return (
            f"  [{node.id}] {node.role} {label!r} "
            f"center=({cx},{cy}) rect=({rect_x},{rect_y},{rect_w},{rect_h}){suffix}"
        )

    @staticmethod
    def _a11y_node_rank(node: "AccessibilityNode") -> tuple[int, int, int, int, str]:
        role = node.role.lower()
        actions = set(node.actions)
        states = set(node.states)
        area = node.width * node.height

        if "press" in actions:
            bucket = 0
        elif "set_text" in actions or "focus" in actions:
            bucket = 1
        elif states.intersection({"focused", "selected", "expanded"}):
            bucket = 2
        elif any(part in role for part in ("row", "cell", "item", "outline", "list")):
            bucket = 3
        elif any(part in role for part in ("statictext", "text")):
            bucket = 4
        elif any(part in role for part in ("window", "group", "scrollarea", "toolbar")):
            bucket = 5
        else:
            bucket = 4

        label = node.label or node.value or node.description
        return (bucket, area, node.depth, int(node.id), label.lower())

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
        context = self._observation_context_text()
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

    def _display_for_window(self, window):
        best = None
        best_area = 0
        for display in self._platform.get_displays():
            left = max(window.x, display.origin_x)
            top = max(window.y, display.origin_y)
            right = min(window.x + window.width, display.origin_x + display.width)
            bottom = min(window.y + window.height, display.origin_y + display.height)
            area = max(0, right - left) * max(0, bottom - top)
            if area > best_area:
                best = display
                best_area = area
        return best

    def _window_intersects_selected_display(self, window) -> bool:
        scale = self._mapper.scale
        return Rect(window.x, window.y, window.width, window.height).intersects(
            Rect(scale.origin_x, scale.origin_y, scale.actual_w, scale.actual_h)
        )

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
            display = args.get("display")
            return self.screenshot(
                query=str(args.get("query", "")),
                display=_safe_int(display) if display is not None else None,
            )
        if name == "click":
            return await self.click(
                _safe_int(args["x"]), _safe_int(args["y"]),
                button=cast(MouseButton, str(args.get("button", "left"))),
                click_count=_safe_int(args.get("click_count", 1)),
                include_screenshot=include_screenshot,
            )
        if name == "mouse_move":
            return await self.mouse_move(
                _safe_int(args["x"]), _safe_int(args["y"]),
                include_screenshot=include_screenshot,
            )
        if name == "drag":
            from_display = args.get("from_display")
            to_display = args.get("to_display")
            return await self.drag(
                _safe_int(args["from_x"]), _safe_int(args["from_y"]),
                _safe_int(args["to_x"]), _safe_int(args["to_y"]),
                from_display=(
                    _safe_int(from_display) if from_display is not None else None
                ),
                to_display=_safe_int(to_display) if to_display is not None else None,
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
                cast(ScrollDirection, str(args["direction"])),
                _safe_int(args.get("amount", 3)),
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
        if name == "list_windows":
            return self.list_windows(app=str(args.get("app", "")))
        if name == "activate_window":
            return await self.activate_window(
                str(args["window_id"]), include_screenshot=include_screenshot,
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


def tool_error_detail(error: Exception) -> str:
    """Short human-readable detail for a tool-call exception.

    Special-cases ``KeyError`` (missing required JSON field) since Python's
    default ``str(KeyError("y"))`` renders as ``"'y'"`` with no context.
    """
    if isinstance(error, KeyError) and error.args:
        return f"missing required field {error.args[0]!r}"
    return str(error)


def tool_error_hint(name: str, input_data: dict[str, Any]) -> str:
    """Actionable hint for a tool-call error, tailored to common mistakes.

    Shared between ``ComputerUseExecutor`` (native Anthropic/OpenAI loop)
    and the MCP server so both surfaces give the model the same guidance
    — e.g. catching a model passing "269, 959" as one field instead of
    separate x/y integers.
    """
    if name in {"click", "mouse_move", "scroll"}:
        x_value = input_data.get("x")
        if "y" not in input_data and isinstance(x_value, str) and "," in x_value:
            return (
                "Pass x and y as separate integer fields, "
                "not as a single comma-separated string, "
                'for example {"x": 100, "y": 200}'
            )
        return "Pass integer x and y fields that match the tool schema"
    if name == "drag":
        for x_field, y_field in (("from_x", "from_y"), ("to_x", "to_y")):
            x_value = input_data.get(x_field)
            if (
                y_field not in input_data
                and isinstance(x_value, str)
                and "," in x_value
            ):
                return (
                    f"Pass {x_field} and {y_field} as separate integer fields, "
                    "not as a single comma-separated string, "
                    f'for example {{"{x_field}": 100, "{y_field}": 200}}'
                )
        return "Pass integer from_x, from_y, to_x, to_y fields that match the tool schema"
    return "Match the tool input to the schema exactly"


def format_tool_error(
    name: str,
    input_data: dict[str, Any],
    input_schema: dict[str, Any] | None,
    error: Exception,
) -> str:
    """Format a rich tool-call error: what failed, what was sent, what's expected.

    Shared by ``ComputerUseExecutor._execute_tool`` (native loop) and
    the MCP server's tool handlers so external CLI agents (Claude Code,
    Codex, ...) get the same actionable feedback as the built-in
    Anthropic/OpenAI computer-use loop instead of a bare exception string.
    ``input_schema`` may be None for tools with no known schema (falls
    back to the basic error without a schema/hint section).
    """
    parts = [
        f"Error executing {name}: {tool_error_detail(error)}.",
        f"Received input: {json.dumps(input_data, ensure_ascii=True, sort_keys=True)}.",
    ]
    if input_schema is not None:
        schema_text = json.dumps(input_schema, ensure_ascii=True, sort_keys=True)
        parts.append(f"Expected schema: {schema_text}.")
        parts.append(f"Hint: {tool_error_hint(name, input_data)}.")
    return " ".join(parts)


# Shared opt-out flag for batched, independent tool calls issued in the same
# turn (MCP hosts like Codex / Claude Code CLI, and any other tool-calling
# client wired directly to this schema). ComputerUseExecutor ignores it and
# decides screenshot inclusion itself (see is_last_tool in its agentic loop).
_INCLUDE_SCREENSHOT_PROP: dict[str, Any] = {
    "type": "boolean",
    "description": (
        "Set to false when this call is one of several independent actions "
        "you are issuing in the same turn and it is NOT the last one — "
        "skips the screenshot in the result to save tokens. Default true."
    ),
}


def _coord_schema(x_desc: str, y_desc: str) -> dict[str, Any]:
    """Helper: JSON schema for an (x, y) coordinate pair."""
    return {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": x_desc},
            "y": {"type": "integer", "description": y_desc},
            "include_screenshot": _INCLUDE_SCREENSHOT_PROP,
        },
        "required": ["x", "y"],
    }


GUI_TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="screenshot",
        description=(
            "Take a screenshot of the selected display. Returns the image plus "
            "compact accessibility context when available. Call this first to "
            "see what's on screen before acting. Pass display to switch the "
            "working display before capture. Optionally pass query to filter "
            "the returned accessibility context by control text; query does "
            "not search the screenshot image."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Optional control text used only to filter the returned "
                        "accessibility context; does not search the screenshot image"
                    ),
                },
                "display": {
                    "type": "integer",
                    "description": (
                        "Optional 1-based display index. Switches the working "
                        "display before capture."
                    ),
                },
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="click",
        description=(
            "Click at the given (x, y) pixel coordinates. Coordinates are "
            "relative to the screenshot image. The image is 1024 px wide and "
            "preserves the selected display's aspect ratio; each screenshot "
            "result states its exact size and valid ranges. Use button to "
            "pick 'left' (default), 'right', or 'middle'. Use click_count=2 "
            "for a double-click (must be a single call, not two click calls)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate (0-1023)"},
                "y": {
                    "type": "integer",
                    "description": "Y coordinate (0 to screenshot height - 1)",
                },
                "button": {
                    "type": "string",
                    "enum": list(_VALID_MOUSE_BUTTONS),
                    "description": "Mouse button to click (default 'left')",
                },
                "click_count": {
                    "type": "integer",
                    "description": (
                        "Number of clicks at this point, e.g. 2 for "
                        "double-click (default 1)"
                    ),
                },
                "include_screenshot": _INCLUDE_SCREENSHOT_PROP,
            },
            "required": ["x", "y"],
        },
    ),
    ToolSpec(
        name="mouse_move",
        description="Move the mouse cursor to (x, y) without clicking.",
        input_schema=_coord_schema("X coordinate", "Y coordinate"),
    ),
    ToolSpec(
        name="drag",
        description=(
            "Drag the left mouse button from (from_x, from_y) to (to_x, to_y). "
            "Each endpoint is relative to the compressed screenshot of its "
            "display. Omit from_display and to_display for a same-display drag. "
            "A successful cross-display drag switches the working display to "
            "to_display and returns its screenshot."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "from_x": {"type": "integer", "description": "Starting X coordinate"},
                "from_y": {"type": "integer", "description": "Starting Y coordinate"},
                "to_x": {"type": "integer", "description": "Ending X coordinate"},
                "to_y": {"type": "integer", "description": "Ending Y coordinate"},
                "from_display": {
                    "type": "integer",
                    "description": (
                        "Optional 1-based display index for the starting coordinates; "
                        "defaults to the working display"
                    ),
                },
                "to_display": {
                    "type": "integer",
                    "description": (
                        "Optional 1-based display index for the ending coordinates; "
                        "defaults to the working display"
                    ),
                },
                "include_screenshot": _INCLUDE_SCREENSHOT_PROP,
            },
            "required": ["from_x", "from_y", "to_x", "to_y"],
        },
    ),
    ToolSpec(
        name="type_text",
        description=(
            "Type the given text string. The text is typed character by character "
            "into whatever field currently has focus. Use click first to focus "
            "the target input field."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
                "include_screenshot": _INCLUDE_SCREENSHOT_PROP,
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
                "include_screenshot": _INCLUDE_SCREENSHOT_PROP,
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
                    "enum": list(_VALID_SCROLL_DIRECTIONS),
                    "description": "Scroll direction",
                },
                "amount": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Number of scroll steps (default 3)",
                },
                "include_screenshot": _INCLUDE_SCREENSHOT_PROP,
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
                "include_screenshot": _INCLUDE_SCREENSHOT_PROP,
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="list_windows",
        description=(
            "List visible windows that can be activated by ID across all displays. "
            "Returns each window's opaque ID, app, title, display, global bounds, "
            "and active state. Optionally filter by an exact stable application "
            "identifier (app, process, executable, or bundle ID). Use "
            "activate_window with a returned ID."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Optional exact stable application identifier",
                },
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="activate_window",
        description=(
            "Activate a specific visible window using an opaque ID returned by "
            "list_windows. Switches the working display to the display containing "
            "most of that window and returns its screenshot. The target receives "
            "keyboard focus, but the OS may also raise sibling windows from the "
            "same application."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "string",
                    "description": "Opaque window ID returned by list_windows",
                },
                "include_screenshot": _INCLUDE_SCREENSHOT_PROP,
            },
            "required": ["window_id"],
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
                    "description": "Exact stable application identifier",
                },
                "include_screenshot": _INCLUDE_SCREENSHOT_PROP,
            },
            "required": ["app"],
        },
    ),
    ToolSpec(
        name="get_active_window",
        description=(
            "Get the currently active window as JSON (window_id, app, title, "
            "pid, display, and global bounds). Cheap focus check that doesn't "
            "burn a screenshot."
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

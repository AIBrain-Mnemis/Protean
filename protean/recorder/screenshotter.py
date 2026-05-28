"""Live screenshot capture for v2 recordings.

When a salient input event fires, capture a single full-display screenshot,
build an overview + detail crop pair, and write the relative paths back onto
the event. The skill-generation pipeline then consumes these images directly,
without any video / ffmpeg involvement.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PIL import Image, ImageDraw

from protean.analyzer.frames import event_xy_to_native, make_overview_and_detail
from protean.platform.base import DisplayInfo, Platform
from protean.recorder.events import EventType, InputEvent

log = logging.getLogger(__name__)

# Salient event types that trigger a screenshot in v2 mode.
SCREENSHOT_TRIGGERS: set[EventType] = {
    EventType.MOUSE_CLICK,
    EventType.MOUSE_DOUBLE_CLICK,
    EventType.MOUSE_DRAG_END,
    EventType.MOUSE_SCROLL,
    EventType.KEY_COMBO,
    EventType.TEXT_INPUT,
    EventType.APP_SWITCH,
}

_MOUSE_POSITIONAL = {
    EventType.MOUSE_CLICK,
    EventType.MOUSE_DOUBLE_CLICK,
    EventType.MOUSE_DRAG_END,
    EventType.MOUSE_SCROLL,
}
_KEYBOARD_FOCUSED = {EventType.TEXT_INPUT, EventType.KEY_COMBO}
_LAST_BOUNDS_TTL = 10.0  # seconds; reuse last click bounds for keyboard events


class ScreenshotCapturer:
    """Capture overview + detail screenshot pairs for salient events."""

    def __init__(
        self,
        platform: Platform,
        display_info: DisplayInfo | None,
        out_dir: Path,
        display_index: int,
    ) -> None:
        self._platform = platform
        self._display_info = display_info
        self._out_dir = out_dir
        self._display_index = display_index
        self._counter = 0
        self._lock = threading.Lock()
        self._last_click_bounds: tuple[int, int, int, int] | None = None
        self._last_click_ts: float = 0.0
        self._out_dir.mkdir(parents=True, exist_ok=True)

    def should_capture(self, event: InputEvent) -> bool:
        return event.event_type in SCREENSHOT_TRIGGERS

    def capture_for(self, event: InputEvent) -> None:
        """Best-effort: take a screenshot, write overview/detail, mutate event."""
        with self._lock:
            self._counter += 1
            idx = self._counter

        native_path = self._out_dir / f"{idx:04d}_full.png"
        try:
            self._platform.capture_display(self._display_index, native_path)
        except Exception as e:
            log.warning("capture_display failed for event %s: %s", event.event_type, e)
            return

        try:
            with Image.open(native_path) as native_img:
                native_img.load()
                if native_img.mode != "RGB":
                    native_img = native_img.convert("RGB")

                display_width = self._display_info.width if self._display_info else 0
                display_origin = (
                    (self._display_info.origin_x, self._display_info.origin_y)
                    if self._display_info
                    else (0, 0)
                )

                try:
                    self._annotate(
                        native_img, event, display_width, display_origin
                    )
                except Exception as e:
                    log.warning("annotate failed: %s", e)

                ev_x = event.x if event.event_type in _MOUSE_POSITIONAL else None
                ev_y = event.y if ev_x is not None else None

                (
                    overview_bytes,
                    _ow,
                    _oh,
                    detail_bytes,
                    _dw,
                    _dh,
                    detail_info,
                ) = make_overview_and_detail(
                    native_img, ev_x, ev_y, display_width, display_origin
                )
        except Exception as e:
            log.warning("overview/detail build failed: %s", e)
            self._safe_unlink(native_path)
            return

        overview_rel = f"screenshots/{idx:04d}_overview.jpg"
        overview_abs = self._out_dir.parent / overview_rel
        try:
            overview_abs.write_bytes(overview_bytes)
            event.screenshot_overview = overview_rel
        except Exception as e:
            log.warning("write overview failed: %s", e)

        if detail_bytes is not None:
            detail_rel = f"screenshots/{idx:04d}_detail.jpg"
            detail_abs = self._out_dir.parent / detail_rel
            try:
                detail_abs.write_bytes(detail_bytes)
                event.screenshot_detail = detail_rel
                event.screenshot_crop_info = detail_info
            except Exception as e:
                log.warning("write detail failed: %s", e)

        self._safe_unlink(native_path)

    @staticmethod
    def _safe_unlink(p: Path) -> None:
        try:
            p.unlink()
        except Exception:
            pass

    def _annotate(
        self,
        img: Image.Image,
        event: InputEvent,
        display_width: int,
        display_origin: tuple[int, int],
    ) -> None:
        """Draw red marker(s) on *img* in-place to visualize the event target."""
        et = event.event_type
        if et == EventType.APP_SWITCH:
            return

        native_w, native_h = img.size
        min_dim = min(native_w, native_h)
        radius = max(20, int(min_dim * 0.018))
        line_w = max(3, int(min_dim * 0.004))
        red = (255, 32, 32)

        draw = ImageDraw.Draw(img)

        bounds_native: tuple[int, int, int, int] | None = None
        raw_bounds = event.metadata.get("element_bounds")
        if (
            isinstance(raw_bounds, (list, tuple))
            and len(raw_bounds) == 4
        ):
            try:
                bx, by, bw, bh = (int(v) for v in raw_bounds)
                if bw > 0 and bh > 0:
                    px1, py1 = event_xy_to_native(
                        bx, by, native_w, display_width, display_origin
                    )
                    px2, py2 = event_xy_to_native(
                        bx + bw, by + bh, native_w, display_width, display_origin
                    )
                    bounds_native = (px1, py1, px2, py2)
            except (ValueError, TypeError):
                pass

        if et in _MOUSE_POSITIONAL:
            cx, cy = event_xy_to_native(
                event.x, event.y, native_w, display_width, display_origin
            )
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                outline=red,
                width=line_w,
            )
            if et == EventType.MOUSE_SCROLL and (event.scroll_dx or event.scroll_dy):
                arrow_len = radius + line_w * 4
                ax = cx + (radius + arrow_len) * (
                    1 if event.scroll_dx > 0 else -1 if event.scroll_dx < 0 else 0
                )
                ay = cy + (radius + arrow_len) * (
                    -1 if event.scroll_dy > 0 else 1 if event.scroll_dy < 0 else 0
                )
                if (ax, ay) != (cx, cy):
                    draw.line([cx, cy, ax, ay], fill=red, width=line_w)

            if bounds_native is not None:
                draw.rectangle(bounds_native, outline=red, width=line_w)
                self._last_click_bounds = bounds_native
                self._last_click_ts = event.timestamp
            elif et in (EventType.MOUSE_CLICK, EventType.MOUSE_DOUBLE_CLICK):
                self._last_click_bounds = None
                self._last_click_ts = event.timestamp
            return

        if et in _KEYBOARD_FOCUSED:
            box = bounds_native
            if box is None and self._last_click_bounds is not None:
                if event.timestamp - self._last_click_ts <= _LAST_BOUNDS_TTL:
                    box = self._last_click_bounds
            if box is not None:
                draw.rectangle(box, outline=red, width=line_w)
            label = (
                f"TEXT: {event.text[:60]}"
                if et == EventType.TEXT_INPUT
                else f"KEY: {'+'.join(event.modifiers + [event.key])}"
            )
            margin = max(8, line_w * 2)
            draw.text((margin, margin), label, fill=red)

"""Input monitor — cross-platform keyboard & mouse event capture.

Uses pynput for cross-platform monitoring. Enriches each event with
active window context from the platform layer.

Window context is resolved **asynchronously** in a dedicated worker
thread so that slow OS calls (``CGWindowListCopyWindowInfo``, etc.)
never block pynput's ``CGEventTap`` callback — preventing macOS from
auto-disabling the tap and silently dropping events.

For mouse events the monitor uses coordinate-based hit-testing
(``Platform.get_window_at_point``) so that clicks inside menu-bar /
accessory-policy apps (e.g. GlobalProtect) are correctly attributed.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from pynput import keyboard, mouse

from protean.platform.base import ClipboardContent, ElementInfo, Platform
from protean.recorder.events import (
    EventType,
    InputEvent,
    MouseButton,
    WindowContext,
)

# Throttle settings (seconds / pixels)
_MOUSE_MOVE_THROTTLE_S = 0.25
_MOUSE_MOVE_THROTTLE_PX = 28
_SCROLL_THROTTLE_S = 0.2

# Clipboard capture settings
_CLIPBOARD_COPY_DELAY_S = 0.15
_CLIPBOARD_PASTE_DELAY_S = 0.05
_CLIPBOARD_MAX_LENGTH = 2000

# Windows VK code → readable name (only used when pynput canonical() demotes
# a Key enum to a raw KeyCode, which lacks .name)
_VK_TO_NAME: dict[int, str] = {
    0x08: "backspace",
    0x09: "tab",
    0x0D: "enter",
    0x10: "shift",
    0x11: "ctrl",
    0x12: "alt",
    0x14: "caps_lock",
    0x1B: "escape",
    0x20: "space",
    0x21: "page_up",
    0x22: "page_down",
    0x23: "end",
    0x24: "home",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x2D: "insert",
    0x2E: "delete",
    0x5B: "cmd",
}
for _i in range(12):
    _VK_TO_NAME[0x70 + _i] = f"f{_i + 1}"

# Sentinel used to signal the resolver thread to shut down.
_SHUTDOWN = object()


class InputMonitor:
    """Monitors keyboard and mouse events, enriched with window context."""

    def __init__(
        self,
        platform: Platform,
        on_event: Callable[[InputEvent], None],
        *,
        record_mouse_move: bool = False,
    ) -> None:
        self._platform = platform
        self._on_event = on_event
        self._start_time: float = 0
        self._mouse_listener: mouse.Listener | None = None
        self._keyboard_listener: keyboard.Listener | None = None
        self._running = False
        self._record_mouse_move = record_mouse_move
        # Throttle state
        self._last_move_time: float = 0
        self._last_move_x: int = 0
        self._last_move_y: int = 0
        # Text accumulation (protected by _text_lock)
        self._text_buffer: list[str] = []
        self._text_start_time: float = 0
        self._text_end_time: float = 0
        self._text_window_key: tuple[str, str] = ("", "")
        self._text_window_context: WindowContext = WindowContext()
        self._text_flush_timer: threading.Timer | None = None
        self._text_lock = threading.Lock()
        # Cached window key for text context tracking
        self._cached_window_key: tuple[str, str] = ("", "")
        self._cached_window_time: float = 0
        # Active modifiers tracking
        self._active_modifiers: set[str] = set()
        # Scroll accumulation (protected by _scroll_lock)
        self._scroll_accum_dx: int = 0
        self._scroll_accum_dy: int = 0
        self._scroll_accum_x: int = 0
        self._scroll_accum_y: int = 0
        self._scroll_accum_start: float = 0
        self._scroll_accum_end: float = 0
        self._scroll_flush_timer: threading.Timer | None = None
        self._scroll_lock = threading.Lock()
        # Drag detection
        self._press_x: int = 0
        self._press_y: int = 0
        self._press_time: float = 0
        # Double-click detection
        self._last_click_time: float = 0
        self._last_click_x: int = 0
        self._last_click_y: int = 0
        self._last_click_button: MouseButton = MouseButton.LEFT
        # Right-click context menu clipboard tracking
        self._pre_context_menu_clipboard: ClipboardContent | None = None
        self._context_menu_time: float = 0
        # Async window-context resolution
        self._resolve_queue: queue.Queue = queue.Queue()
        self._resolver_thread: threading.Thread | None = None
        self._last_resolved_process: str = ""  # for APP_SWITCH detection
        # Element pre-fetch: runs element_at in parallel with the resolver
        # so that the accessibility query starts at click-time, not after
        # get_window_at_point finishes (saving 50-200ms of UI drift).
        self._element_queue: queue.Queue = queue.Queue()
        self._element_thread: threading.Thread | None = None
        self._element_results: dict[int, ElementInfo | None] = {}
        self._element_results_ready: dict[int, threading.Event] = {}
        self._element_lock = threading.Lock()
        self._click_id_seq: int = 0

    def start(self, start_time: float) -> None:
        self._start_time = start_time
        self._running = True

        # Start the resolver thread *before* the listeners so it's ready
        # to process events immediately.
        self._resolver_thread = threading.Thread(
            target=self._resolver_loop, daemon=True, name="window-resolver"
        )
        self._resolver_thread.start()

        self._element_thread = threading.Thread(
            target=self._element_prefetch_loop, daemon=True, name="element-prefetch"
        )
        self._element_thread.start()

        self._mouse_listener = mouse.Listener(
            on_click=self._on_click,
            on_move=self._on_move,
            on_scroll=self._on_scroll,
        )
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )

        self._mouse_listener.start()
        self._keyboard_listener.start()

    def stop(self) -> None:
        self._flush_scroll_buffer()
        self._flush_text_buffer()
        self._running = False
        if self._mouse_listener:
            self._mouse_listener.stop()
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        # Signal and wait for the resolver thread to drain & exit.
        self._resolve_queue.put(_SHUTDOWN)
        self._element_queue.put(_SHUTDOWN)
        if self._resolver_thread and self._resolver_thread.is_alive():
            self._resolver_thread.join(timeout=3)
        if self._element_thread and self._element_thread.is_alive():
            self._element_thread.join(timeout=3)

    def _elapsed(self) -> float:
        return time.monotonic() - self._start_time

    # ── Async window-context resolution ─────────────────

    def _element_prefetch_loop(self) -> None:
        """Background thread that runs element_at as soon as a click happens.

        Runs in parallel with the resolver so the accessibility query starts
        at click-time rather than after get_window_at_point completes.

        A queue item with x=None means "use the currently focused control"
        (used for keyboard events where pointer position is irrelevant).
        """
        import sys
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.ole32.CoInitialize(None)
            except Exception:
                pass
        while True:
            item = self._element_queue.get()
            if item is _SHUTDOWN:
                break
            click_id: int
            ex: int | None
            ey: int | None
            click_id, ex, ey = item
            elem: ElementInfo | None = None
            try:
                if ex is None or ey is None:
                    focused = getattr(self._platform, "element_focused", None)
                    if callable(focused):
                        elem = focused()
                else:
                    elem = self._platform.element_at(ex, ey)
            except Exception:
                pass
            with self._element_lock:
                self._element_results[click_id] = elem
                ready = self._element_results_ready.get(click_id)
            if ready is not None:
                ready.set()

    def _request_element_prefetch(self, x: int, y: int) -> int:
        """Dispatch an element_at pre-fetch from the pynput callback.

        Returns a click_id the resolver can use to collect the result.
        """
        click_id = self._click_id_seq
        self._click_id_seq += 1
        ready = threading.Event()
        with self._element_lock:
            self._element_results_ready[click_id] = ready
        self._element_queue.put((click_id, x, y))
        return click_id

    def _request_focused_prefetch(self) -> int:
        """Dispatch a focused-control pre-fetch (for keyboard events).

        Returns a click_id the resolver/collector uses to retrieve the result.
        """
        click_id = self._click_id_seq
        self._click_id_seq += 1
        ready = threading.Event()
        with self._element_lock:
            self._element_results_ready[click_id] = ready
        self._element_queue.put((click_id, None, None))
        return click_id

    def _collect_element_result(self, click_id: int) -> ElementInfo | None:
        """Collect a pre-fetched element_at result, waiting up to 1.5s.

        TODO: Downgrade UIA to best-effort (~200ms, non-blocking). Rely on
        detail-crop screenshots + vision model for element identification
        instead of UIA labels. UIA varies too much across apps/versions/OS.
        """
        with self._element_lock:
            ready = self._element_results_ready.get(click_id)
        if ready is not None:
            ready.wait(timeout=1.5)
        with self._element_lock:
            self._element_results_ready.pop(click_id, None)
            return self._element_results.pop(click_id, None)

    def _resolver_loop(self) -> None:
        """Background thread that resolves window context for queued events.

        Each item on the queue is either the ``_SHUTDOWN`` sentinel or a tuple
        ``(event, mouse_x_or_None, mouse_y_or_None, click_id_or_None)``.
        When mouse coords are provided we use ``get_window_at_point`` for
        precise hit-testing; otherwise we fall back to ``get_active_window``.
        """
        while True:
            item = self._resolve_queue.get()
            if item is _SHUTDOWN:
                break
            event: InputEvent
            mx: int | None
            my: int | None
            click_id: int | None
            event, mx, my, click_id = item
            try:
                win = None
                # Skip window resolution if already captured at event time
                if not event.window.process_name:
                    if mx is not None and my is not None:
                        try:
                            win = self._platform.get_window_at_point(mx, my)
                        except Exception:
                            pass
                    if win is None:
                        win = self._platform.get_active_window()
                    # When clicking a background window, Windows hasn't
                    # finished the foreground switch yet — retry once.
                    if win and not win.process_name:
                        time.sleep(0.15)
                        win = self._platform.get_active_window()
                    if win:
                        event.window = WindowContext(
                            pid=win.pid,
                            process_name=win.process_name,
                            window_title=win.window_title,
                            bundle_id=win.bundle_id,
                        )
            except Exception:
                pass
            # Attach pre-fetched accessibility element for click events
            if click_id is not None:
                try:
                    elem = self._collect_element_result(click_id)
                    if elem is not None and elem.role:
                        event.metadata["element_role"] = elem.role
                        if elem.label:
                            event.metadata["element_label"] = elem.label
                        if elem.width > 0 and elem.height > 0:
                            event.metadata["element_bounds"] = [
                                int(elem.center_x - elem.width / 2),
                                int(elem.center_y - elem.height / 2),
                                int(elem.width),
                                int(elem.height),
                            ]
                except Exception:
                    pass
            # Detect app switches based on resolved window context
            cur_process = event.window.process_name
            if (
                cur_process
                and self._last_resolved_process
                and cur_process != self._last_resolved_process
            ):
                app_switch = InputEvent(
                    timestamp=event.timestamp,
                    event_type=EventType.APP_SWITCH,
                    window=WindowContext(
                        pid=event.window.pid,
                        process_name=cur_process,
                        window_title=event.window.window_title,
                        bundle_id=event.window.bundle_id,
                    ),
                )
                if self._running:
                    self._on_event(app_switch)
            if cur_process:
                self._last_resolved_process = cur_process
            # Emit the now-enriched event.
            if self._running or event.event_type == EventType.TEXT_INPUT:
                self._on_event(event)

    def _emit_with_context(
        self,
        event: InputEvent,
        *,
        mouse_x: int | None = None,
        mouse_y: int | None = None,
        click_id: int | None = None,
    ) -> None:
        """Enqueue *event* for async window-context resolution + emission."""
        self._resolve_queue.put((event, mouse_x, mouse_y, click_id))

    # ── Mouse handlers ───────────────────────────────────

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        btn = MouseButton.LEFT
        if button == mouse.Button.right:
            btn = MouseButton.RIGHT
        elif button == mouse.Button.middle:
            btn = MouseButton.MIDDLE

        ix, iy = int(x), int(y)

        if pressed:
            # Mouse button pressed — record position for potential drag detection
            self._flush_text_buffer()
            self._press_x = ix
            self._press_y = iy
            self._press_time = time.monotonic()

            # Double-click detection
            now = time.monotonic()
            dt = now - self._last_click_time
            dx_click = abs(ix - self._last_click_x)
            dy_click = abs(iy - self._last_click_y)
            is_double = (
                dt < 0.4
                and dx_click < 10
                and dy_click < 10
                and btn == self._last_click_button
            )

            self._last_click_time = now
            self._last_click_x = ix
            self._last_click_y = iy
            self._last_click_button = btn

            if is_double:
                event_type = EventType.MOUSE_DOUBLE_CLICK
                self._last_click_time = 0  # prevent triple → double+double
            else:
                event_type = EventType.MOUSE_CLICK

            event = InputEvent(
                timestamp=self._elapsed(),
                event_type=event_type,
                x=ix,
                y=iy,
                button=btn,
            )

            # Right-click context menu: snapshot clipboard for later comparison
            if btn == MouseButton.RIGHT:
                self._snapshot_clipboard_for_context_menu()
            elif self._pre_context_menu_clipboard is not None:
                # Left click after right-click → might be selecting a menu item
                self._check_context_menu_clipboard(event)

            # Dispatch element_at immediately so it runs in parallel with
            # the resolver's get_window_at_point call.
            cid = self._request_element_prefetch(ix, iy)
            self._emit_with_context(event, mouse_x=ix, mouse_y=iy, click_id=cid)
        else:
            # Mouse button released — check if this was a drag
            dx = abs(ix - self._press_x)
            dy = abs(iy - self._press_y)
            if dx > 10 or dy > 10:
                # Significant movement between press and release → drag.
                # Emit drag_start at press coordinates (retroactive) then
                # drag_end at release coordinates so compaction can pair them.
                drag_start_ts = self._press_time - self._start_time
                if drag_start_ts < 0:
                    drag_start_ts = 0
                start_cid = self._request_element_prefetch(self._press_x, self._press_y)
                self._emit_with_context(
                    InputEvent(
                        timestamp=drag_start_ts,
                        event_type=EventType.MOUSE_DRAG_START,
                        x=self._press_x,
                        y=self._press_y,
                        button=btn,
                    ),
                    mouse_x=self._press_x,
                    mouse_y=self._press_y,
                    click_id=start_cid,
                )
                end_cid = self._request_element_prefetch(ix, iy)
                self._emit_with_context(
                    InputEvent(
                        timestamp=self._elapsed(),
                        event_type=EventType.MOUSE_DRAG_END,
                        x=ix,
                        y=iy,
                        button=btn,
                    ),
                    mouse_x=ix,
                    mouse_y=iy,
                    click_id=end_cid,
                )

    def _on_move(self, x: int, y: int) -> None:
        if not self._record_mouse_move:
            return

        now = time.monotonic()
        dx = abs(x - self._last_move_x)
        dy = abs(y - self._last_move_y)
        dt = now - self._last_move_time if self._last_move_time else 999

        if dt < _MOUSE_MOVE_THROTTLE_S and (dx + dy) < _MOUSE_MOVE_THROTTLE_PX:
            return

        self._last_move_time = now
        self._last_move_x = int(x)
        self._last_move_y = int(y)

        self._emit_with_context(
            InputEvent(
                timestamp=self._elapsed(),
                event_type=EventType.MOUSE_MOVE,
                x=int(x),
                y=int(y),
            ),
            mouse_x=int(x),
            mouse_y=int(y),
        )

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        with self._scroll_lock:
            self._scroll_accum_dx += int(dx)
            self._scroll_accum_dy += int(dy)
            self._scroll_accum_x = int(x)
            self._scroll_accum_y = int(y)
            now = self._elapsed()
            if self._scroll_accum_start == 0:
                self._scroll_accum_start = now
            self._scroll_accum_end = now
            if self._scroll_flush_timer:
                self._scroll_flush_timer.cancel()
            self._scroll_flush_timer = threading.Timer(
                _SCROLL_THROTTLE_S, self._flush_scroll_buffer
            )
            self._scroll_flush_timer.daemon = True
            self._scroll_flush_timer.start()

    def _flush_scroll_buffer(self) -> None:
        with self._scroll_lock:
            if self._scroll_flush_timer:
                self._scroll_flush_timer.cancel()
                self._scroll_flush_timer = None
            dx = self._scroll_accum_dx
            dy = self._scroll_accum_dy
            x = self._scroll_accum_x
            y = self._scroll_accum_y
            ts = self._scroll_accum_start
            end_ts = self._scroll_accum_end
            self._scroll_accum_dx = 0
            self._scroll_accum_dy = 0
            self._scroll_accum_start = 0
            self._scroll_accum_end = 0
        if dx == 0 and dy == 0:
            return
        cid = self._request_element_prefetch(x, y)
        self._emit_with_context(
            InputEvent(
                timestamp=ts,
                end_timestamp=end_ts,
                event_type=EventType.MOUSE_SCROLL,
                x=x,
                y=y,
                scroll_dx=dx,
                scroll_dy=dy,
            ),
            mouse_x=x,
            mouse_y=y,
            click_id=cid,
        )

    # ── Keyboard handlers ────────────────────────────────

    def _key_name(self, key: Any) -> str:
        # Normalize left/right modifier variants (e.g. ctrl_l/ctrl_r → ctrl)
        # so downstream handling is platform-agnostic. On Windows pynput reports
        # Ctrl as Key.ctrl_l by default, which previously bypassed modifier
        # tracking and caused Ctrl+C/Ctrl+V to be lost.
        if self._keyboard_listener is not None:
            try:
                key = self._keyboard_listener.canonical(key)
            except Exception:
                pass
        if hasattr(key, "char") and key.char:
            return key.char
        if hasattr(key, "name"):
            return key.name
        vk = getattr(key, "vk", None)
        if vk is not None and vk in _VK_TO_NAME:
            return _VK_TO_NAME[vk]
        return str(key)

    def _on_key_press(self, key: Any) -> None:
        name = self._key_name(key)

        # Track modifiers
        if name in ("cmd", "ctrl", "alt", "shift"):
            self._active_modifiers.add(name)
            return

        # If modifiers are held, it's a key combo — unless it's just
        # shift + a printable character (typing, not a shortcut).
        if self._active_modifiers:
            # shift-only + printable char → treat as typed text (e.g. shift+/ = ?)
            orig_char = key.char if hasattr(key, "char") else None
            if (
                self._active_modifiers == {"shift"}
                and orig_char
                and len(orig_char) == 1
            ):
                text_char = orig_char
                cur_window = self._current_window_key()
                pending_event: InputEvent | None = None
                with self._text_lock:
                    if self._text_buffer and cur_window != self._text_window_key:
                        pending_event = self._extract_text_event_locked()
                    now = self._elapsed()
                    if not self._text_buffer:
                        self._text_start_time = now
                        self._text_window_key = cur_window
                        self._snapshot_window_for_text()
                    self._text_end_time = now
                    self._text_buffer.append(text_char)
                if pending_event:
                    self._emit_with_context(pending_event)
                self._schedule_text_flush()
                return

            self._flush_text_buffer()
            mods = self._active_modifiers
            event = InputEvent(
                timestamp=self._elapsed(),
                event_type=EventType.KEY_COMBO,
                key=name,
                modifiers=sorted(mods),
            )
            # Capture window context NOW — clipboard delay or resolver lag
            # can cause get_active_window() to return a different window.
            self._snapshot_window(event)
            is_copy = (
                (name in ("c", "x") and ("ctrl" in mods or "cmd" in mods))
                or (name == "insert" and "ctrl" in mods)
                or (name == "delete" and "shift" in mods)
            )
            is_paste = (
                (name == "v" and ("ctrl" in mods or "cmd" in mods))
                or (name == "insert" and "shift" in mods)
            )
            if is_copy or is_paste:
                delay = _CLIPBOARD_COPY_DELAY_S if is_copy else _CLIPBOARD_PASTE_DELAY_S
                self._schedule_clipboard_capture(event, delay, is_copy=is_copy)
            else:
                cid = self._request_focused_prefetch()
                self._emit_with_context(event, click_id=cid)
            return

        # Printable character (or space) → accumulate into text buffer
        text_char = key.char if hasattr(key, "char") and key.char else None
        if text_char is None and name == "space":
            text_char = " "
        if text_char:
            cur_window = self._current_window_key()
            pending_event: InputEvent | None = None
            with self._text_lock:
                if self._text_buffer and cur_window != self._text_window_key:
                    pending_event = self._extract_text_event_locked()
                now = self._elapsed()
                if not self._text_buffer:
                    self._text_start_time = now
                    self._text_window_key = cur_window
                    self._snapshot_window_for_text()
                self._text_end_time = now
                self._text_buffer.append(text_char)
            if pending_event:
                self._emit_with_context(pending_event)
            self._schedule_text_flush()
            return

        # Non-printable key
        self._flush_text_buffer()
        key_event = InputEvent(
            timestamp=self._elapsed(),
            event_type=EventType.KEY_PRESS,
            key=name,
        )
        self._snapshot_window(key_event)
        cid = self._request_focused_prefetch()
        self._emit_with_context(key_event, click_id=cid)

    def _on_key_release(self, key: Any) -> None:
        name = self._key_name(key)
        self._active_modifiers.discard(name)

    # ── Clipboard capture ────────────────────────────────

    @staticmethod
    def _attach_clipboard_metadata(
        event: InputEvent, cb: ClipboardContent
    ) -> None:
        """Write ClipboardContent fields into event.metadata."""
        if cb.kind == "text" and cb.text:
            event.metadata["clipboard_kind"] = "text"
            event.metadata["clipboard_text"] = cb.text[:_CLIPBOARD_MAX_LENGTH]
            if len(cb.text) > _CLIPBOARD_MAX_LENGTH:
                event.metadata["clipboard_truncated"] = True
        elif cb.kind == "files" and cb.files:
            event.metadata["clipboard_kind"] = "files"
            event.metadata["clipboard_files"] = cb.files
        elif cb.kind == "image":
            event.metadata["clipboard_kind"] = "image"
            if cb.image_width and cb.image_height:
                event.metadata["clipboard_image_size"] = [
                    cb.image_width,
                    cb.image_height,
                ]

    def _schedule_clipboard_capture(
        self, event: InputEvent, delay: float, *, is_copy: bool = False
    ) -> None:
        """Read clipboard after a delay and attach content to the event.

        For copy operations, retries up to 3 times with 100ms back-off because
        apps (especially browsers) may write to the clipboard asynchronously.
        """

        def _read_and_emit() -> None:
            attempts = 3 if is_copy else 1
            for i in range(attempts):
                try:
                    cb = self._platform.get_clipboard()
                    if cb.kind != "empty":
                        self._attach_clipboard_metadata(event, cb)
                        break
                except Exception:
                    pass
                if i < attempts - 1:
                    time.sleep(0.1)
            self._emit_with_context(event)

        timer = threading.Timer(delay, _read_and_emit)
        timer.daemon = True
        timer.start()

    def _snapshot_clipboard_for_context_menu(self) -> None:
        """Snapshot current clipboard on right-click for later comparison."""
        try:
            self._pre_context_menu_clipboard = self._platform.get_clipboard()
            self._context_menu_time = time.monotonic()
        except Exception:
            self._pre_context_menu_clipboard = None

    def _check_context_menu_clipboard(self, event: InputEvent) -> None:
        """Compare clipboard after context menu click; attach if changed."""
        prev = self._pre_context_menu_clipboard
        self._pre_context_menu_clipboard = None
        if prev is None:
            return
        # Ignore if too long since right-click (menu dismissed without action)
        if time.monotonic() - self._context_menu_time > 3.0:
            return
        try:
            cur = self._platform.get_clipboard()
            if cur.kind == "empty":
                return
            changed = (
                cur.kind != prev.kind
                or cur.text != prev.text
                or cur.files != prev.files
            )
            if changed:
                self._attach_clipboard_metadata(event, cur)
        except Exception:
            pass

    # ── Text buffer ──────────────────────────────────────

    def _snapshot_window(self, event: InputEvent) -> None:
        """Pre-fill event.window with the current active window.

        Called at keypress time so the event records which window was active
        at the moment the key was pressed, not when the resolver gets to it.
        """
        try:
            win = self._platform.get_active_window()
            if win:
                event.window = WindowContext(
                    pid=win.pid,
                    process_name=win.process_name,
                    window_title=win.window_title,
                    bundle_id=win.bundle_id,
                )
        except Exception:
            pass

    def _snapshot_window_for_text(self) -> None:
        """Store a window context snapshot for the current text buffer.

        Called when the first character is typed into a new buffer.
        Caller must hold _text_lock.
        """
        try:
            win = self._platform.get_active_window()
            if win:
                self._text_window_context = WindowContext(
                    pid=win.pid,
                    process_name=win.process_name,
                    window_title=win.window_title,
                    bundle_id=win.bundle_id,
                )
            else:
                self._text_window_context = WindowContext()
        except Exception:
            self._text_window_context = WindowContext()

    def _current_window_key(self) -> tuple[str, str]:
        """Quick snapshot of (process_name, window_title) for context tracking.

        Caches the result for 0.5s to avoid slow OS calls on every keypress
        (macOS CGWindowListCopyWindowInfo can take 10-50ms and would cause
        the CGEventTap to be auto-disabled if called too frequently).
        """
        now = time.monotonic()
        if now - self._cached_window_time < 0.5:
            return self._cached_window_key
        try:
            win = self._platform.get_active_window()
            if win:
                self._cached_window_key = (win.process_name, win.window_title)
            else:
                self._cached_window_key = ("", "")
        except Exception:
            self._cached_window_key = ("", "")
        self._cached_window_time = now
        return self._cached_window_key

    def _schedule_text_flush(self) -> None:
        with self._text_lock:
            if self._text_flush_timer:
                self._text_flush_timer.cancel()
            self._text_flush_timer = threading.Timer(5.0, self._flush_text_buffer)
            self._text_flush_timer.daemon = True
            self._text_flush_timer.start()

    def _flush_text_buffer(self) -> None:
        with self._text_lock:
            event = self._extract_text_event_locked()
        if event:
            cid = self._request_focused_prefetch()
            self._emit_with_context(event, click_id=cid)

    def _extract_text_event_locked(self) -> InputEvent | None:
        """Extract pending text as an InputEvent. Caller must hold _text_lock."""
        if self._text_flush_timer:
            self._text_flush_timer.cancel()
            self._text_flush_timer = None
        if not self._text_buffer:
            return None
        text = "".join(self._text_buffer)
        start_ts = self._text_start_time
        end_ts = self._text_end_time
        win_ctx = self._text_window_context
        self._text_buffer.clear()
        self._text_start_time = 0
        self._text_end_time = 0
        self._text_window_key = ("", "")
        self._text_window_context = WindowContext()
        return InputEvent(
            timestamp=start_ts,
            event_type=EventType.TEXT_INPUT,
            end_timestamp=end_ts,
            text=text,
            window=win_ctx,
        )

"""macOS platform backend.

Uses:
- Quartz/CoreGraphics for window info, screenshots, screen recording
- NSWorkspace for process/app info
- CGEvent for input simulation
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import AppKit
from AppKit import (
    NSData,
    NSPasteboard,
    NSPasteboardTypeString,
    NSRunningApplication,
    NSWorkspace,
)
from ApplicationServices import (
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyElementAtPosition,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
)
from CoreFoundation import kCFBooleanTrue
from Foundation import NSArray, NSBundle
from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    CGDisplayBounds,
    CGDisplayPixelsWide,
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventCreateScrollWheelEvent,
    CGEventGetFlags,
    CGEventGetIntegerValueField,
    CGEventKeyboardSetUnicodeString,
    CGEventMaskBit,
    CGEventPost,
    CGEventSetIntegerValueField,
    CGEventTapCreate,
    CGGetActiveDisplayList,
    CGMainDisplayID,
    CGPointMake,
    CGWindowListCopyWindowInfo,
    NSEvent,
    kCFRunLoopCommonModes,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskControl,
    kCGEventFlagMaskShift,
    kCGEventKeyDown,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseDragged,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGEventOtherMouseDown,
    kCGEventOtherMouseUp,
    kCGEventRightMouseDown,
    kCGEventRightMouseUp,
    kCGHeadInsertEventTap,
    kCGHIDEventTap,
    kCGKeyboardEventKeycode,
    kCGMouseButtonCenter,
    kCGMouseButtonLeft,
    kCGMouseButtonRight,
    kCGMouseEventClickState,
    kCGNullWindowID,
    kCGScrollEventUnitLine,
    kCGSessionEventTap,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionOnScreenOnly,
)

from protean.platform.base import (
    AccessibilityNode,
    AccessibilitySnapshot,
    ClipboardContent,
    DisplayInfo,
    ElementInfo,
    MouseButton,
    Platform,
    Rect,
    ScrollDirection,
    WindowInfo,
    app_identifier_matches,
)

log = logging.getLogger(__name__)


def _extract_ax_point(val: object) -> tuple[float | None, float | None]:
    """Extract (x, y) from an AXValueRef representing a CGPoint."""
    # Try AXValueGetValue first (may not be available in all PyObjC versions)
    try:
        from HIServices import AXValueGetValue, kAXValueTypeCGPoint

        ok, point = AXValueGetValue(val, kAXValueTypeCGPoint, None)
        if ok:
            return (float(point.x), float(point.y))
    except (ImportError, TypeError, AttributeError):
        pass

    # Fallback: parse from string representation
    # Format: "<AXValue ...> {value = x:-216.000000 y:-674.000000 type = kAXValueCGPointType}"
    try:
        m = re.search(r"x:([-\d.]+)\s*y:([-\d.]+)", str(val))
        if m:
            return (float(m.group(1)), float(m.group(2)))
    except (TypeError, ValueError):
        pass

    return (None, None)


def _extract_ax_size(val: object) -> tuple[float | None, float | None]:
    """Extract (width, height) from an AXValueRef representing a CGSize."""
    # Try AXValueGetValue first
    try:
        from HIServices import AXValueGetValue, kAXValueTypeCGSize

        ok, size = AXValueGetValue(val, kAXValueTypeCGSize, None)
        if ok:
            return (float(size.width), float(size.height))
    except (ImportError, TypeError, AttributeError):
        pass

    # Fallback: parse from string representation
    # Format: "<AXValue ...> {value = w:511.000000 h:34.000000 type = kAXValueCGSizeType}"
    try:
        m = re.search(r"w:([-\d.]+)\s*h:([-\d.]+)", str(val))
        if m:
            return (float(m.group(1)), float(m.group(2)))
    except (TypeError, ValueError):
        pass

    return (None, None)


class MacOSPlatform(Platform):
    """macOS implementation of the Platform protocol."""

    def __init__(self) -> None:
        self._recording_process: subprocess.Popen | None = None
        self._recording_path: Path | None = None
        self._audio_process: subprocess.Popen | None = None

    @property
    def name(self) -> str:
        return "macos"

    def _list_avfoundation_devices(self) -> str:
        """Get ffmpeg avfoundation device list."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stderr
        except Exception:
            return ""

    def _find_audio_device(self) -> str | None:
        """Find a suitable virtual audio device for recording app audio.

        Checks avfoundation audio devices for known app audio sources.
        Returns the device name, or None if no suitable device found.
        """
        devices = self._list_avfoundation_devices()

        # Known app audio device patterns (order = priority)
        # Each app that exposes a virtual audio device gets matched here.
        known_patterns = [
            "Microsoft Teams Audio",  # Teams
            "ZoomAudioDevice",  # Zoom
            "WebEx",  # Cisco WebEx
            "Discord",  # Discord
            "Slack",  # Slack
            "BlackHole",  # Generic virtual audio (fallback)
        ]
        for pattern in known_patterns:
            if pattern in devices:
                return pattern
        return None

    # ── Window info ──────────────────────────────────────

    def get_window_at_point(self, x: int, y: int) -> WindowInfo | None:
        """Return the topmost window containing the given screen coordinates.

        Uses CGWindowListCopyWindowInfo to hit-test *all* on-screen windows
        (including menu-bar / accessory-policy apps like GlobalProtect that
        NSWorkspace.activeApplication() ignores).  Windows are returned in
        front-to-back z-order, so the first geometric hit is the topmost one.
        """
        options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
        window_list = CGWindowListCopyWindowInfo(options, kCGNullWindowID)

        for win in window_list:
            if float(win.get("kCGWindowAlpha", 1.0)) <= 0:
                continue
            bounds = win.get("kCGWindowBounds", {})
            wx = int(bounds.get("X", 0))
            wy = int(bounds.get("Y", 0))
            ww = int(bounds.get("Width", 0))
            wh = int(bounds.get("Height", 0))

            # Skip tiny / zero-size windows (e.g. status-bar icons)
            if ww < 2 or wh < 2:
                continue

            if wx <= x < wx + ww and wy <= y < wy + wh:
                pid = win.get("kCGWindowOwnerPID", 0)
                return WindowInfo(
                    pid=pid,
                    process_name=win.get("kCGWindowOwnerName", ""),
                    window_title=win.get("kCGWindowName", ""),
                    window_id=str(win.get("kCGWindowNumber", "")),
                    bundle_id=self._bundle_id_for_pid(pid),
                    x=wx,
                    y=wy,
                    width=ww,
                    height=wh,
                )

        # No geometric hit — fall back to the active-app method
        return self.get_active_window()

    @staticmethod
    def _bundle_id_for_pid(pid: int) -> str:
        """Return the bundle identifier for a given PID, or '' on failure."""
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return ""
        return app.bundleIdentifier() or ""

    def get_active_window(self) -> WindowInfo | None:
        active_app = NSWorkspace.sharedWorkspace().activeApplication()
        if not active_app:
            return None

        pid = active_app["NSApplicationProcessIdentifier"]
        app_name = active_app.get("NSApplicationName", "")
        bundle_id = active_app.get("NSApplicationBundleIdentifier", "")

        windows = self._visible_cg_windows(pid)
        focused_rect = self._focused_ax_window_rect(pid)
        if focused_rect is not None:
            matches = [win for win in windows if self._cg_window_rect(win) == focused_rect]
            if len(matches) == 1:
                return self._window_info(matches[0], bundle_id=bundle_id)

        if windows:
            return self._window_info(windows[0], bundle_id=bundle_id)

        return WindowInfo(pid=pid, process_name=app_name, window_title="", bundle_id=bundle_id)

    def list_windows(self) -> list[WindowInfo]:
        self._ensure_accessibility()
        results: list[WindowInfo] = []
        for win in self._visible_cg_windows():
            pid = int(win.get("kCGWindowOwnerPID", 0))
            bundle_id = self._bundle_id_for_pid(pid)
            if not bundle_id:
                continue
            rect = self._cg_window_rect(win)
            if not self._cg_rect_is_unique(pid, rect):
                continue
            if self._ax_window_for_rect(pid, rect) is None:
                continue
            results.append(self._window_info(win, bundle_id=bundle_id))
        return results

    def activate_window(self, window_id: str) -> WindowInfo:
        self._ensure_accessibility()
        try:
            target_id = int(window_id)
        except ValueError as error:
            raise ValueError(f"Invalid macOS window ID: {window_id!r}") from error

        matches = [
            win
            for win in self._visible_cg_windows()
            if int(win.get("kCGWindowNumber", 0)) == target_id
        ]
        if len(matches) != 1:
            raise RuntimeError(f"Window {window_id!r} is not visible")
        target = matches[0]
        pid = int(target.get("kCGWindowOwnerPID", 0))
        target_rect = self._cg_window_rect(target)
        if not self._cg_rect_is_unique(pid, target_rect):
            raise RuntimeError(f"Window {window_id!r} is not uniquely addressable")
        ax_window = self._ax_window_for_rect(pid, target_rect)
        if ax_window is None:
            raise RuntimeError(f"Window {window_id!r} is not uniquely addressable")

        self._activate_pid(pid)
        error = AXUIElementPerformAction(ax_window, "AXRaise")
        if error != 0:
            raise RuntimeError(f"AXRaise failed for window {window_id!r}: {error}")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            active = self.get_active_window()
            if active is not None and active.window_id == window_id:
                return active
            time.sleep(0.05)
        actual = self.get_active_window()
        raise RuntimeError(
            f"Window {window_id!r} did not become active; active window is {actual}"
        )

    @staticmethod
    def _cg_window_rect(win: dict) -> tuple[int, int, int, int]:
        bounds = win.get("kCGWindowBounds", {})
        return (
            int(bounds.get("X", 0)),
            int(bounds.get("Y", 0)),
            int(bounds.get("Width", 0)),
            int(bounds.get("Height", 0)),
        )

    def _visible_cg_windows(self, pid: int | None = None) -> list[dict]:
        options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
        windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID)
        results: list[dict] = []
        for win in windows:
            if pid is not None and int(win.get("kCGWindowOwnerPID", 0)) != pid:
                continue
            if int(win.get("kCGWindowLayer", 999)) != 0:
                continue
            if float(win.get("kCGWindowAlpha", 1.0)) <= 0:
                continue
            _, _, width, height = self._cg_window_rect(win)
            if width < 2 or height < 2:
                continue
            results.append(win)
        return results

    def _cg_rect_is_unique(self, pid: int, rect: tuple[int, int, int, int]) -> bool:
        return sum(
            self._cg_window_rect(win) == rect
            for win in self._visible_cg_windows(pid)
        ) == 1

    def _ax_window_for_rect(self, pid: int, rect: tuple[int, int, int, int]):
        app_ref = AXUIElementCreateApplication(pid)
        try:
            error, windows = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
        except Exception:
            return None
        if error != 0 or not windows:
            return None

        matches = []
        for window in windows:
            try:
                error, role = AXUIElementCopyAttributeValue(window, "AXRole", None)
                if error != 0 or role != "AXWindow":
                    continue
                error, minimized = AXUIElementCopyAttributeValue(window, "AXMinimized", None)
                if error == 0 and bool(minimized):
                    continue
                error, position = AXUIElementCopyAttributeValue(window, "AXPosition", None)
                if error != 0:
                    continue
                error, size = AXUIElementCopyAttributeValue(window, "AXSize", None)
                if error != 0:
                    continue
                point = _extract_ax_point(position)
                dimensions = _extract_ax_size(size)
                px, py = point
                width, height = dimensions
                if px is None or py is None or width is None or height is None:
                    continue
                window_rect = (int(px), int(py), int(width), int(height))
                if window_rect != rect:
                    continue
                error, actions = AXUIElementCopyActionNames(window, None)
                if error == 0 and "AXRaise" in (actions or []):
                    matches.append(window)
            except Exception:
                continue
        return matches[0] if len(matches) == 1 else None

    def _focused_ax_window_rect(self, pid: int) -> tuple[int, int, int, int] | None:
        app_ref = AXUIElementCreateApplication(pid)
        try:
            error, window = AXUIElementCopyAttributeValue(app_ref, "AXFocusedWindow", None)
            if error != 0 or window is None:
                return None
            error, position = AXUIElementCopyAttributeValue(window, "AXPosition", None)
            if error != 0:
                return None
            error, size = AXUIElementCopyAttributeValue(window, "AXSize", None)
            if error != 0:
                return None
        except Exception:
            return None
        point = _extract_ax_point(position)
        dimensions = _extract_ax_size(size)
        px, py = point
        width, height = dimensions
        if px is None or py is None or width is None or height is None:
            return None
        return int(px), int(py), int(width), int(height)

    def _window_info(self, win: dict, *, bundle_id: str) -> WindowInfo:
        x, y, width, height = self._cg_window_rect(win)
        pid = int(win.get("kCGWindowOwnerPID", 0))
        return WindowInfo(
            pid=pid,
            process_name=str(win.get("kCGWindowOwnerName", "")),
            window_title=str(win.get("kCGWindowName", "")),
            window_id=str(win.get("kCGWindowNumber", "")),
            bundle_id=bundle_id,
            x=x,
            y=y,
            width=width,
            height=height,
            app_identifiers=self._running_app_identifiers(
                NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            ),
        )

    def _activate_pid(self, pid: int) -> None:
        script = (
            'tell application "System Events" to set frontmost of '
            f'(first application process whose unix id is {pid}) to true'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"Application PID {pid} could not be activated: {detail}")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            active = NSWorkspace.sharedWorkspace().activeApplication()
            if active and int(active["NSApplicationProcessIdentifier"]) == pid:
                return
            time.sleep(0.05)
        raise RuntimeError(f"Application PID {pid} did not become active")

    def list_notifications(self) -> list[WindowInfo]:
        """List notification/overlay windows (non-layer-0)."""
        options = (
            kCGWindowListOptionOnScreenOnly
            | kCGWindowListExcludeDesktopElements
        )
        window_list = CGWindowListCopyWindowInfo(options, kCGNullWindowID)
        results = []
        for win in window_list:
            layer = win.get("kCGWindowLayer", 0)
            if layer <= 0:
                continue
            bounds = win.get("kCGWindowBounds", {})
            results.append(
                WindowInfo(
                    pid=win.get("kCGWindowOwnerPID", 0),
                    process_name=win.get("kCGWindowOwnerName", ""),
                    window_title=win.get("kCGWindowName", ""),
                    window_id=str(win.get("kCGWindowNumber", "")),
                    x=int(bounds.get("X", 0)),
                    y=int(bounds.get("Y", 0)),
                    width=int(bounds.get("Width", 0)),
                    height=int(bounds.get("Height", 0)),
                )
            )
        return results

    # ── Display info ─────────────────────────────────────

    def get_displays(self) -> list[DisplayInfo]:
        max_displays = 16
        err, display_ids, count = CGGetActiveDisplayList(max_displays, None, None)
        if err != 0:
            return []
        main_id = CGMainDisplayID()
        results = []
        for idx, did in enumerate(display_ids[:count], 1):
            bounds = CGDisplayBounds(did)
            logical_w = int(bounds.size.width)
            pixel_w = CGDisplayPixelsWide(did)
            scale = pixel_w / logical_w if logical_w > 0 else 1.0
            results.append(
                DisplayInfo(
                    display_id=did,
                    display_index=idx,
                    width=logical_w,
                    height=int(bounds.size.height),
                    origin_x=int(bounds.origin.x),
                    origin_y=int(bounds.origin.y),
                    scale_factor=scale,
                    is_primary=(did == main_id),
                )
            )
        return results

    def get_cursor_position(self) -> tuple[int, int]:
        loc = NSEvent.mouseLocation()
        # NSEvent gives bottom-left origin; convert to top-left
        main_bounds = CGDisplayBounds(CGMainDisplayID())
        return int(loc.x), int(main_bounds.size.height - loc.y)

    # ── Screen recording ─────────────────────────────────
    #
    # Video: screencapture (-k for click markers)
    # Audio: ffmpeg via avfoundation (auto-detects app audio device, e.g. "Microsoft Teams Audio")
    # Merged after stop.

    def start_screen_recording(
        self,
        output_path: Path,
        display_index: int = 1,
        *,
        show_clicks: bool = True,
        capture_audio: bool = False,
    ) -> None:
        if self._recording_process is not None:
            raise RuntimeError("Recording already in progress")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Video via screencapture (supports click markers)
        cmd = ["screencapture", "-x", "-v", f"-D{display_index}"]
        if show_clicks:
            cmd.append("-k")
        cmd.append(str(output_path))

        self._recording_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._recording_path = output_path
        self._audio_process = None

        # Audio via ffmpeg — auto-detect app audio device (e.g. "Microsoft Teams Audio")
        if capture_audio:
            audio_dev = self._find_audio_device()
            if audio_dev:
                audio_path = output_path.with_suffix(".audio.m4a")
                self._audio_process = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "avfoundation",
                        "-i",
                        f":{audio_dev}",
                        "-acodec",
                        "aac",
                        str(audio_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

    def stop_screen_recording(self) -> Path | None:
        if self._recording_process is None:
            return None

        # Stop video
        self._recording_process.send_signal(signal.SIGINT)
        try:
            self._recording_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._recording_process.kill()
            self._recording_process.wait(timeout=5)

        # Stop audio if running
        if self._audio_process is not None:
            self._audio_process.send_signal(signal.SIGINT)
            try:
                self._audio_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._audio_process.kill()
            self._audio_process = None

        path = self._recording_path
        self._recording_process = None
        self._recording_path = None

        if not path or not path.exists() or path.stat().st_size == 0:
            return None

        # Merge audio into video if audio was recorded
        audio_path = path.with_suffix(".audio.m4a")
        if audio_path.exists() and audio_path.stat().st_size > 0:
            merged_path = path.with_suffix(".merged.mov")
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-i",
                    str(audio_path),
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(merged_path),
                ],
                capture_output=True,
                timeout=60,
            )
            if result.returncode == 0 and merged_path.exists():
                merged_path.rename(path)
            audio_path.unlink(missing_ok=True)

        return path

    def capture_display(self, display_index: int, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "screencapture", "-x",
            "-D", str(display_index),
        ]
        # Let screencapture output JPEG directly when the path ends with .jpg/.jpeg
        suffix = output_path.suffix.lower()
        if suffix in (".jpg", ".jpeg"):
            cmd.extend(["-t", "jpg"])
        cmd.append(str(output_path))
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="ignore").strip()
            raise RuntimeError(
                f"screencapture failed: {stderr or result.returncode}"
            )

    # ── Power / session management ───────────────────────

    @contextlib.contextmanager
    def keep_awake(self) -> Iterator[None]:
        """Hold a power assertion via `caffeinate -dimsu -w <pid>` for the
        duration of the block.

        The `-w <pid>` form makes caffeinate auto-exit when our process dies,
        so we never leak the assertion. Falls back to a no-op if `caffeinate`
        is missing from PATH.
        """
        if shutil.which("caffeinate") is None:
            log.warning("caffeinate not found; keep-awake disabled on this host")
            yield
            return
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                ["caffeinate", "-dimsu", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.debug("Keep-awake enabled (caffeinate pid=%s)", proc.pid)
            yield
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except Exception:
                    log.debug("Failed to terminate caffeinate", exc_info=True)

    # ── Input simulation ─────────────────────────────────

    def click(
        self, x: int, y: int, button: MouseButton = "left", click_count: int = 1,
    ) -> None:
        point = CGPointMake(x, y)
        if button == "right":
            down_type, up_type, button_const = (
                kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight,
            )
        elif button == "middle":
            down_type, up_type, button_const = (
                kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGMouseButtonCenter,
            )
        else:
            down_type, up_type, button_const = (
                kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft,
            )
        for click_state in range(1, click_count + 1):
            down = CGEventCreateMouseEvent(None, down_type, point, button_const)
            CGEventSetIntegerValueField(down, kCGMouseEventClickState, click_state)
            up = CGEventCreateMouseEvent(None, up_type, point, button_const)
            CGEventSetIntegerValueField(up, kCGMouseEventClickState, click_state)
            CGEventPost(kCGHIDEventTap, down)
            CGEventPost(kCGHIDEventTap, up)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        """Drag from one point to another using CGEvent mouse-down/dragged/up.

        Posts intermediate ``kCGEventLeftMouseDragged`` events along the
        path so apps that track drag position (drop-target highlighting,
        outline/table reordering) see a real gesture instead of a jump.
        """
        down = CGEventCreateMouseEvent(
            None, kCGEventLeftMouseDown, CGPointMake(from_x, from_y), kCGMouseButtonLeft,
        )
        CGEventPost(kCGHIDEventTap, down)
        steps = 10
        for step in range(1, steps + 1):
            ix = from_x + (to_x - from_x) * step // steps
            iy = from_y + (to_y - from_y) * step // steps
            dragged = CGEventCreateMouseEvent(
                None, kCGEventLeftMouseDragged, CGPointMake(ix, iy), kCGMouseButtonLeft,
            )
            CGEventPost(kCGHIDEventTap, dragged)
            time.sleep(0.01)
        up = CGEventCreateMouseEvent(
            None, kCGEventLeftMouseUp, CGPointMake(to_x, to_y), kCGMouseButtonLeft,
        )
        CGEventPost(kCGHIDEventTap, up)

    def scroll(
        self,
        x: int,
        y: int,
        direction: ScrollDirection = "down",
        amount: int = 3,
    ) -> None:
        """Scroll at (x, y). direction: up/down/left/right."""
        # Move cursor to position first
        self.move_cursor(x, y)
        dy, dx = 0, 0
        if direction == "down":
            dy = -amount
        elif direction == "up":
            dy = amount
        elif direction == "right":
            dx = amount
        elif direction == "left":
            dx = -amount
        scroll_event = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2, dy, dx)
        CGEventPost(kCGHIDEventTap, scroll_event)

    def move_cursor(self, x: int, y: int) -> None:
        point = CGPointMake(x, y)
        move = CGEventCreateMouseEvent(None, kCGEventMouseMoved, point, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, move)

    def type_text(self, text: str) -> None:
        """Type text into the focused element via clipboard paste.

        Uses NSPasteboard + AppleScript Cmd+V.  AppleScript's keystroke route
        reaches Chromium-based browsers (Teams, Edge, Chrome) where CGEvent
        posts at kCGHIDEventTap are silently dropped.

        Saves and restores the previous clipboard content so the user's
        clipboard is not clobbered.
        """
        pb = NSPasteboard.generalPasteboard()

        # Save current clipboard (all types).
        old_items: list[tuple[str, bytes]] = []
        old_types = pb.types()
        if old_types:
            for t in old_types:
                d = pb.dataForType_(t)
                if d:
                    old_items.append((t, bytes(d)))

        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

        # Cmd+V via AppleScript — works in browsers where CGEvent is blocked.
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "v" using command down'],
            timeout=5,
        )

        # Wait for the target app to read the clipboard, then restore.
        time.sleep(0.15)
        pb.clearContents()
        if old_items:
            for t, raw in old_items:
                pb.setData_forType_(NSData.dataWithBytes_length_(raw, len(raw)), t)

    def get_clipboard(self) -> ClipboardContent:
        """Read structured clipboard content."""
        try:
            # Check available types via 'clipboard info'
            info_result = subprocess.run(
                ["osascript", "-e", "clipboard info"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            info = info_result.stdout if info_result.returncode == 0 else ""

            # Files — check for file URL type
            if "furl" in info or "public.file-url" in info:
                file_result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'set fileList to paragraphs of (do shell script '
                        '"pbpaste -Prefer public.file-url 2>/dev/null || true")\n'
                        "set output to {}\n"
                        "repeat with f in fileList\n"
                        '  if f as text is not "" then set end of output to f as text\n'
                        "end repeat\n"
                        'set AppleScript\'s text item delimiters to "\\n"\n'
                        "return output as text",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if file_result.returncode == 0 and file_result.stdout.strip():
                    files = [
                        f
                        for f in file_result.stdout.strip().split("\n")
                        if f.strip()
                    ]
                    if files:
                        return ClipboardContent(kind="files", files=files)

            # Image — check for image types
            if "PNGf" in info or "TIFF" in info or "public.png" in info:
                # Get image dimensions via sips on a temp file
                try:
                    import tempfile

                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        tmp_path = tmp.name
                    subprocess.run(
                        [
                            "osascript",
                            "-e",
                            f'set imgData to the clipboard as «class PNGf»\n'
                            f'set fp to open for access POSIX file'
                            f' "{tmp_path}" with write permission\n'
                            f"write imgData to fp\n"
                            f"close access fp",
                        ],
                        capture_output=True,
                        timeout=3,
                    )
                    sips = subprocess.run(
                        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", tmp_path],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    os.unlink(tmp_path)
                    w, h = 0, 0
                    for line in sips.stdout.split("\n"):
                        if "pixelWidth" in line:
                            w = int(line.split(":")[-1].strip())
                        elif "pixelHeight" in line:
                            h = int(line.split(":")[-1].strip())
                    if w and h:
                        return ClipboardContent(
                            kind="image", image_width=w, image_height=h
                        )
                except Exception:
                    pass
                return ClipboardContent(kind="image")

            # Text fallback
            text_result = subprocess.run(
                ["pbpaste"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            text = text_result.stdout if text_result.returncode == 0 else ""
            if text:
                return ClipboardContent(kind="text", text=text)

            return ClipboardContent()
        except Exception:
            return ClipboardContent()

    def key_press(self, *keys: str) -> None:
        """Press a keyboard shortcut.

        Uses AppleScript when modifier keys are involved (Chromium browsers
        silently drop CGEvent modifier combos posted at kCGHIDEventTap).
        Falls back to CGEvent for plain keys without modifiers to avoid the
        ~100ms AppleScript overhead.

        Accepts modifier names (cmd, shift, ctrl, alt/option) and a trigger key.
        Examples: key_press("cmd", "s"), key_press("cmd", "shift", "e"),
                  key_press("return"), key_press("tab")
        """
        _MODIFIER_NAMES = {
            "cmd", "command", "shift", "ctrl", "control", "alt", "option",
        }

        has_modifier = any(k.lower() in _MODIFIER_NAMES for k in keys)
        if has_modifier:
            self._key_press_applescript(*keys)
        else:
            self._key_press_cgevent(*keys)

    def _key_press_cgevent(self, *keys: str) -> None:
        """Press a key via CGEvent (no modifiers)."""
        # macOS virtual keycodes (kVK_* from Events.h)
        _KEYCODE_MAP = {
            "return": 36, "enter": 36, "tab": 48, "escape": 53, "esc": 53,
            "delete": 51, "backspace": 51, "forward_delete": 117,
            "space": 49, "up": 126, "down": 125, "left": 123, "right": 124,
            "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
            "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
            "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
            "f11": 103, "f12": 111,
        }

        # Printable ASCII → macOS keycode (US QWERTY layout)
        _CHAR_KEYCODE = {
            "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5,
            "h": 4, "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45,
            "o": 31, "p": 35, "q": 12, "r": 15, "s": 1, "t": 17, "u": 32,
            "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
            "0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
            "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
            "-": 27, "=": 24, "[": 33, "]": 30, "\\": 42,
            ";": 41, "'": 39, ",": 43, ".": 47, "/": 44, "`": 50,
        }

        for k in keys:
            kl = k.lower()
            trigger_char: str | None = None
            if kl in _KEYCODE_MAP:
                keycode = _KEYCODE_MAP[kl]
            elif kl in _CHAR_KEYCODE:
                keycode = _CHAR_KEYCODE[kl]
                trigger_char = kl
            elif len(kl) == 1:
                self._key_press_applescript(k)
                return
            else:
                log.warning("Unknown key: %s", kl)
                return

            key_down = CGEventCreateKeyboardEvent(None, keycode, True)
            if trigger_char is not None:
                CGEventKeyboardSetUnicodeString(key_down, len(trigger_char), trigger_char)
            CGEventPost(kCGHIDEventTap, key_down)

            key_up = CGEventCreateKeyboardEvent(None, keycode, False)
            if trigger_char is not None:
                CGEventKeyboardSetUnicodeString(key_up, len(trigger_char), trigger_char)
            CGEventPost(kCGHIDEventTap, key_up)

    def _key_press_applescript(self, *keys: str) -> None:
        """Press a keyboard shortcut via AppleScript.

        Uses ``keystroke`` for printable characters, ``key code`` for special
        keys (Return, Tab, arrows, etc.).  Works in Chromium browsers where
        CGEvent modifier combos are silently dropped.
        """
        _MODIFIER_MAP = {
            "cmd": "command down", "command": "command down",
            "shift": "shift down", "ctrl": "control down", "control": "control down",
            "alt": "option down", "option": "option down",
        }

        # AppleScript key codes for non-printable keys
        _SPECIAL_KEYCODE = {
            "return": 36, "enter": 36, "tab": 48, "escape": 53, "esc": 53,
            "delete": 51, "backspace": 51, "forward_delete": 117,
            "space": 49, "up": 126, "down": 125, "left": 123, "right": 124,
            "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
            "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
            "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
            "f11": 103, "f12": 111,
        }

        modifiers = []
        trigger = None
        for k in keys:
            kl = k.lower()
            if kl in _MODIFIER_MAP:
                modifiers.append(_MODIFIER_MAP[kl])
            else:
                trigger = kl
        if trigger is None:
            return
        using = f" using {{{', '.join(modifiers)}}}" if modifiers else ""
        trigger_lower = trigger.lower()
        if trigger_lower in _SPECIAL_KEYCODE:
            code = _SPECIAL_KEYCODE[trigger_lower]
            script = f'tell application "System Events" to key code {code}{using}'
        else:
            trigger_esc = _escape_applescript(trigger)
            script = f'tell application "System Events" to keystroke "{trigger_esc}"{using}'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)

    # ── Accessibility (UI element discovery) ─────────────

    def _find_ax_element(
        self, app: str, label: str, *, role: str = "", timeout: float = 3.0
    ) -> object | None:
        """DFS search for an AXUIElement matching label.

        Uses role-based pruning and a time budget to handle deep
        web-rendered trees (e.g. WebKit inside Outlook).

        Returns the AXUIElement reference, or None if not found.
        """
        pid = self._find_app_pid(app)
        if pid is None:
            return None

        app_ref = AXUIElementCreateApplication(pid)

        # Force Chromium-based apps to fully expose web content AX tree
        try:
            AXUIElementSetAttributeValue(
                app_ref, "AXEnhancedUserInterface", True,
            )
        except Exception:
            pass

        _ACTIONABLE_ROLES = {
            "AXButton", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton",
            "AXPopUpButton", "AXComboBox", "AXSlider", "AXMenuItem", "AXMenuBarItem",
            "AXLink", "AXTab", "AXStaticText", "AXImage",
        }
        _SKIP_ROLES = {"AXMenuBar", "AXMenu"}

        max_depth = 30
        deadline = time.monotonic() + timeout

        stack: list[tuple[object, int]] = [(app_ref, 0)]

        while stack:
            if time.monotonic() > deadline:
                break
            el, depth = stack.pop()
            if depth > max_depth:
                continue

            err_r, el_role = AXUIElementCopyAttributeValue(el, "AXRole", None)
            role_str = str(el_role) if el_role else ""

            if role_str in _SKIP_ROLES:
                continue

            if role_str in _ACTIONABLE_ROLES:
                for attr in ("AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue"):
                    try:
                        err, val = AXUIElementCopyAttributeValue(el, attr, None)
                    except Exception:
                        continue
                    if val and label.lower() in str(val).lower():
                        if role and role_str != role:
                            continue
                        return el

            try:
                err_c, children = AXUIElementCopyAttributeValue(el, "AXChildren", None)
            except Exception:
                continue
            if children:
                for i in range(len(children) - 1, -1, -1):
                    stack.append((children[i], depth + 1))

        return None

    def find_element(
        self, app: str, label: str, *, role: str = "", timeout: float = 3.0
    ) -> tuple[int, int] | None:
        """Find a UI element by label and return its center (x, y)."""
        el = self._find_ax_element(app, label, role=role, timeout=timeout)
        if el is None:
            return None

        try:
            err1, pos_val = AXUIElementCopyAttributeValue(el, "AXPosition", None)
            err2, size_val = AXUIElementCopyAttributeValue(el, "AXSize", None)
        except Exception:
            return None
        pos_x, pos_y = _extract_ax_point(pos_val)
        w, h = _extract_ax_size(size_val)
        if pos_x is not None and pos_y is not None and w is not None and h is not None:
            return (int(pos_x + w / 2), int(pos_y + h / 2))
        return None

    def ax_press(self, app: str, label: str, *, role: str = "") -> bool:
        """Find element and perform AXPress — reliable for web-rendered controls."""
        el = self._find_ax_element(app, label, role=role)
        if el is None:
            return False

        err = AXUIElementPerformAction(el, "AXPress")
        return err == 0

    def select_option(self, app: str, label: str, value: str) -> bool:
        """Select an option from a dropdown/popup.

        Strategies (tried in order):
        1. AXPress: activate + click to open → find menuWindow → DFS for
           matching text → AXPress deepest-first until popup closes
        2. AXSelectedRows: find AXTable in menuWindow → set AXSelectedRows
           on matching row → Enter to confirm (handles scrollable lists)
        """
        el = self._find_ax_element(app, label, role="AXPopUpButton")
        if el is None:
            el = self._find_ax_element(app, label)
        if el is None:
            return False

        pid = self._find_app_pid(app)
        if pid is None:
            return False
        app_ref = AXUIElementCreateApplication(pid)

        _, pos_val = AXUIElementCopyAttributeValue(el, "AXPosition", None)
        _, size_val = AXUIElementCopyAttributeValue(el, "AXSize", None)
        px, py = _extract_ax_point(pos_val)
        w, h = _extract_ax_size(size_val)
        if px is None or py is None or w is None or h is None:
            return False
        cx, cy = int(px + w / 2), int(py + h / 2)

        def _menu_window():
            _, wins = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
            if wins:
                for win in wins:
                    _, t = AXUIElementCopyAttributeValue(win, "AXTitle", None)
                    if str(t) == "menuWindow":
                        return win
            return None

        def _popup_gone() -> bool:
            time.sleep(0.3)
            return _menu_window() is None

        def _match_text(node) -> bool:
            for attr in ("AXTitle", "AXValue", "AXDescription"):
                try:
                    _, v = AXUIElementCopyAttributeValue(node, attr, None)
                except Exception:
                    continue
                if v and value.lower() in str(v).lower():
                    return True
            return False

        def _try_press_deep(node) -> bool:
            """AXPress from deepest child up; return True if popup closes."""
            try:
                _, nc = AXUIElementCopyAttributeValue(node, "AXChildren", None)
            except Exception:
                nc = None
            if nc:
                for child in nc:
                    if _try_press_deep(child):
                        return True
            err = AXUIElementPerformAction(node, "AXPress")
            if err == 0 and _popup_gone():
                return True
            return False

        # Check if dropdown is already open (agent may have clicked it first)
        mw = _menu_window()
        if mw is None:
            # Open the dropdown
            self.activate_app(app)
            time.sleep(0.3)
            self.click(cx, cy)
            time.sleep(0.5)
            mw = _menu_window()
        if mw is None:
            return False

        # --- Strategy 1: AXSelectedRows + Enter (preferred) ---
        # Works for both scrollable and non-scrollable menuWindow popups.
        # AXSelectedRows scrolls the target row into view and highlights it.
        _, sa_list = AXUIElementCopyAttributeValue(mw, "AXChildren", None)
        if sa_list:
            for sa in sa_list:
                _, sr = AXUIElementCopyAttributeValue(sa, "AXRole", None)
                if str(sr) != "AXScrollArea":
                    continue
                _, tl = AXUIElementCopyAttributeValue(sa, "AXChildren", None)
                if not tl:
                    continue
                for tbl in tl:
                    _, tr = AXUIElementCopyAttributeValue(tbl, "AXRole", None)
                    if str(tr) != "AXTable":
                        continue
                    _, rows = AXUIElementCopyAttributeValue(tbl, "AXChildren", None)
                    if not rows:
                        continue
                    for row in rows:
                        _, rr = AXUIElementCopyAttributeValue(row, "AXRole", None)
                        if str(rr) != "AXRow":
                            continue
                        rs = [row]
                        found = False
                        while rs:
                            rn = rs.pop()
                            if _match_text(rn):
                                found = True
                                break
                            try:
                                _, rnc = AXUIElementCopyAttributeValue(
                                    rn, "AXChildren", None
                                )
                            except Exception:
                                continue
                            if rnc:
                                rs.extend(rnc)
                        if found:
                            AXUIElementSetAttributeValue(
                                tbl, "AXSelectedRows",
                                NSArray.arrayWithObject_(row),
                            )
                            if _popup_gone():
                                return True
                            self.key_press("return")
                            if _popup_gone():
                                return True
                            break

        # --- Strategy 2: menuWindow DFS → AXPress (fallback) ---
        mw = _menu_window()
        if mw is None:
            # Strategy 1 closed it but we missed the check — treat as success
            return True

        stack = [(mw, 0)]
        visited = 0
        while stack and visited < 500:
            node, depth = stack.pop()
            visited += 1
            if _match_text(node):
                if _try_press_deep(node):
                    return True
                break
            try:
                _, nc = AXUIElementCopyAttributeValue(node, "AXChildren", None)
            except Exception:
                continue
            if nc:
                for ci in range(len(nc) - 1, -1, -1):
                    stack.append((nc[ci], depth + 1))

        # All failed — dismiss
        if _menu_window():
            self.key_press("escape")
        return False

    def find_menu_item(self, app: str, menu_path: str) -> bool:
        """Click a menu item via AppleScript.

        Supports nested menus: "File > New > Meeting" (any depth).
        Activates the app first, then navigates the menu bar.
        """
        parts = [p.strip() for p in menu_path.split(">")]
        if len(parts) < 2:
            return False

        # Find the process name as seen by System Events
        process_name = self._find_process_name(app)
        if not process_name:
            process_name = app

        # Build nested AppleScript for arbitrary depth:
        # click menu item "Meeting" of menu 1 of menu item "New"
        # of menu 1 of menu bar item "File" of menu bar 1
        # parts = ["File", "New", "Meeting"]
        # → menu bar item "File" of menu bar 1
        # → menu item "New" of menu 1 of (above)
        # → menu item "Meeting" of menu 1 of (above)
        chain = f'menu bar item "{_escape_applescript(parts[0])}" of menu bar 1'
        for part in parts[1:-1]:
            chain = f'menu item "{_escape_applescript(part)}" of menu 1 of {chain}'
        menu_script = f'click menu item "{_escape_applescript(parts[-1])}" of menu 1 of {chain}'

        script = (
            f'tell application "{_escape_applescript(app)}" to activate\n'
            f"delay 0.5\n"
            f'tell application "System Events"\n'
            f'  tell process "{_escape_applescript(process_name)}"\n'
            f"    {menu_script}\n"
            f"  end tell\n"
            f"end tell"
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 and result.stderr:
            # Log for debugging — stderr has AppleScript error details
            pass
        return result.returncode == 0

    def list_menu_items(self, app: str, menu_path: str = "") -> list[str]:
        """List menu items at a given path via AppleScript."""
        process_name = self._find_process_name(app) or app

        if not menu_path:
            script = (
                f'tell application "System Events" to tell process '
                f'"{_escape_applescript(process_name)}" to get '
                f'name of every menu bar item of menu bar 1'
            )
        else:
            parts = [pt.strip() for pt in menu_path.split(">")]
            chain = f'menu bar item "{_escape_applescript(parts[0])}" of menu bar 1'
            for part in parts[1:]:
                chain = f'menu item "{_escape_applescript(part)}" of menu 1 of {chain}'
            script = (
                f'tell application "System Events" to tell process '
                f'"{_escape_applescript(process_name)}" to get '
                f'name of every menu item of menu 1 of {chain}'
            )

        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        # AppleScript returns comma-separated list
        raw = result.stdout.strip()
        if not raw:
            return []
        return [item.strip() for item in raw.split(", ")]

    def list_elements(self, app: str, max_depth: int = 8) -> list[str]:
        """List visible UI elements in the frontmost window via AX API."""
        pid = self._find_app_pid(app)
        if pid is None:
            return []

        app_ref = AXUIElementCreateApplication(pid)
        max_depth = min(max_depth, 15)

        # Force Chromium-based apps to fully expose web content AX tree
        try:
            AXUIElementSetAttributeValue(
                app_ref, "AXEnhancedUserInterface", True,
            )
        except Exception:
            pass

        _ACTIONABLE_ROLES = {
            "AXButton", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton",
            "AXPopUpButton", "AXComboBox", "AXSlider", "AXMenuItem", "AXMenuBarItem",
            "AXLink", "AXTab", "AXTabGroup", "AXToolbar", "AXStaticText",
        }
        _SKIP_ROLES = {"AXMenuBar", "AXMenu"}

        results: list[str] = []
        deadline = time.monotonic() + 3.0

        # DFS with time budget and pruning
        stack: list[tuple[object, int]] = [(app_ref, 0)]
        while stack and len(results) < 80:
            if time.monotonic() > deadline:
                break
            el, depth = stack.pop()
            if depth > max_depth:
                continue

            err_r, role = AXUIElementCopyAttributeValue(el, "AXRole", None)
            role_str = str(role) if err_r == 0 and role else ""

            if role_str in _SKIP_ROLES:
                continue

            if role_str in _ACTIONABLE_ROLES:
                err_t, title = AXUIElementCopyAttributeValue(el, "AXTitle", None)
                err_d, desc = AXUIElementCopyAttributeValue(el, "AXDescription", None)
                err_v, value = AXUIElementCopyAttributeValue(el, "AXValue", None)

                label = str(title) if err_t == 0 and title else ""
                desc_str = str(desc) if err_d == 0 and desc else ""
                val_str = str(value)[:50] if err_v == 0 and value else ""

                display = label or desc_str or val_str
                if display:
                    indent = "  " * depth
                    results.append(f"{indent}{role_str}: {display!r}")

            try:
                err_c, children = AXUIElementCopyAttributeValue(el, "AXChildren", None)
            except Exception:
                continue
            if children:
                for i in range(len(children) - 1, -1, -1):
                    stack.append((children[i], depth + 1))

        return results

    def get_element_role(self, app: str, label: str) -> str | None:
        el = self._find_ax_element(app, label)
        if el is None:
            return None
        _, role = AXUIElementCopyAttributeValue(el, "AXRole", None)
        return str(role) if role else None

    def accessibility_snapshot(
        self,
        query: str = "",
        *,
        app: str = "",
        visible_bounds: Rect | None = None,
        max_nodes: int,
        max_visited: int,
        timeout: float,
    ) -> AccessibilitySnapshot:
        requested_app = app.strip()
        if requested_app:
            pid = self._find_app_pid(requested_app)
            if pid is None:
                return AccessibilitySnapshot(
                    app=requested_app,
                    unavailable_reason=f"application is not running: {requested_app}",
                )
            app_name = self._find_process_name(requested_app) or requested_app
            window_title = ""
        else:
            window = self.get_active_window()
            if window is None:
                return AccessibilitySnapshot(unavailable_reason="no active window")
            pid = window.pid
            app_name = window.process_name
            window_title = window.window_title

        if pid is None:
            return AccessibilitySnapshot(unavailable_reason="no active window")

        app_ref = AXUIElementCreateApplication(pid)
        try:
            AXUIElementSetAttributeValue(app_ref, "AXEnhancedUserInterface", True)
        except Exception:
            pass

        roots: list[object] = []
        try:
            err, focused = AXUIElementCopyAttributeValue(app_ref, "AXFocusedWindow", None)
            if err == 0 and focused is not None:
                roots.append(focused)
        except Exception:
            pass
        if not roots:
            try:
                err, wins = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
                if err == 0 and wins:
                    roots.extend(wins)
            except Exception:
                pass
        if not roots:
            roots.append(app_ref)

        skip_roles = {"AXMenuBar", "AXMenu"}
        default_roles = {
            "AXButton", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton",
            "AXPopUpButton", "AXComboBox", "AXSlider", "AXMenuItem", "AXMenuBarItem",
            "AXLink", "AXTab", "AXTabGroup", "AXToolbar",
        }
        label_attrs = ("AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue")
        query_lower = query.lower().strip()
        deadline = time.monotonic() + timeout
        max_depth = 25
        nodes: list[AccessibilityNode] = []
        seen: set[tuple[str, str, int, int, int, int]] = set()
        node_index = 1
        visited = 0
        truncated = False
        stack: list[tuple[object, int]] = [(root, 0) for root in roots]

        def _attr(node: object, name: str) -> object | None:
            try:
                err, val = AXUIElementCopyAttributeValue(node, name, None)
            except Exception:
                return None
            return val if err == 0 and val is not None else None

        def _text(node: object, name: str, limit: int = 120) -> str:
            val = _attr(node, name)
            if val is None:
                return ""
            text = str(val).strip()
            return text[:limit]

        def _flag(node: object, name: str) -> bool:
            val = _attr(node, name)
            return bool(val) if val is not None else False

        while stack:
            if time.monotonic() > deadline:
                truncated = True
                break
            visited += 1
            if visited > max_visited:
                truncated = True
                break
            raw, depth = stack.pop()
            if depth > max_depth:
                truncated = True
                continue

            role = _text(raw, "AXRole", 80)
            if role in skip_roles:
                continue

            pos_val = _attr(raw, "AXPosition")
            size_val = _attr(raw, "AXSize")
            px, py = _extract_ax_point(pos_val) if pos_val is not None else (None, None)
            width, height = _extract_ax_size(size_val) if size_val is not None else (None, None)
            rect: Rect | None = None
            if px is not None and py is not None and width is not None and height is not None:
                rect = Rect(int(px), int(py), int(width), int(height))

            visible = visible_bounds is None or rect is None or rect.intersects(visible_bounds)

            label = ""
            value = ""
            description = ""
            query_hit = False
            for attr in label_attrs:
                text = _text(raw, attr)
                if not text:
                    continue
                if attr == "AXValue":
                    value = text
                elif attr == "AXDescription":
                    description = text
                if not label:
                    label = text
                if query_lower and query_lower in text.lower():
                    query_hit = True
                    break

            query_matches = not query_lower or query_hit or query_lower in role.lower()

            states: list[str] = []
            if _flag(raw, "AXEnabled"):
                states.append("enabled")
            if _flag(raw, "AXFocused"):
                states.append("focused")
            if _flag(raw, "AXSelected"):
                states.append("selected")
            if _flag(raw, "AXExpanded"):
                states.append("expanded")

            include_default = role in default_roles or any(
                state in states for state in ("focused", "selected", "expanded")
            )
            if (
                query_matches
                and visible
                and (query_lower or include_default)
                and rect is not None
                and rect.width >= 5
                and rect.height >= 5
                and (label or value or description)
            ):
                x = rect.x
                y = rect.y
                w = rect.width
                h = rect.height
                key = (role, label or value or description, x, y, w, h)
                if key not in seen:
                    actions: list[str] = []
                    if role in {
                        "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
                        "AXMenuItem", "AXMenuBarItem", "AXLink", "AXTab",
                    }:
                        actions.append("press")
                    if role in {"AXTextField", "AXTextArea", "AXComboBox"}:
                        actions.extend(["focus", "set_text"])

                    seen.add(key)
                    nodes.append(AccessibilityNode(
                        id=str(node_index),
                        role=role,
                        raw_role=role,
                        label=label,
                        value=value,
                        description=description,
                        x=x,
                        y=y,
                        width=w,
                        height=h,
                        depth=depth,
                        states=tuple(states),
                        actions=tuple(actions),
                    ))
                    node_index += 1
                    if len(nodes) >= max_nodes:
                        truncated = bool(stack)
                        break

            try:
                err, children = AXUIElementCopyAttributeValue(raw, "AXChildren", None)
            except Exception:
                continue
            if err == 0 and children:
                for i in range(len(children) - 1, -1, -1):
                    stack.append((children[i], depth + 1))

        return AccessibilitySnapshot(
            app=app_name,
            window_title=window_title,
            nodes=nodes,
            truncated=truncated,
        )

    def element_at(self, x: int, y: int) -> ElementInfo | None:
        """Get the accessibility element at global screen coordinates.

        Walks up from the hit-test leaf to the nearest actionable ancestor
        when the leaf itself has no useful label.
        """
        _ACTIONABLE_ROLES = {
            "AXButton", "AXTextField", "AXTextArea", "AXCheckBox",
            "AXRadioButton", "AXPopUpButton", "AXComboBox", "AXSlider",
            "AXMenuItem", "AXMenuBarItem", "AXLink", "AXTab",
            "AXStaticText", "AXImage", "AXToolbar",
        }
        _STOP_ROLES = {"AXWindow", "AXApplication", "AXWebArea"}

        system = AXUIElementCreateSystemWide()
        err, el = AXUIElementCopyElementAtPosition(
            system, float(x), float(y), None,
        )
        if err != 0 or el is None:
            return None

        def _get_label(node) -> str:
            for attr in ("AXTitle", "AXDescription", "AXValue"):
                _, val = AXUIElementCopyAttributeValue(node, attr, None)
                if val and str(val).strip():
                    return str(val)
            return ""

        def _get_role(node) -> str:
            _, r = AXUIElementCopyAttributeValue(node, "AXRole", None)
            return str(r) if r else ""

        # Walk up to find the nearest meaningful element
        current = el
        for _ in range(15):
            role_str = _get_role(current)
            if role_str in _STOP_ROLES:
                break
            label = _get_label(current)
            if role_str in _ACTIONABLE_ROLES and label:
                el = current
                break
            if (
                role_str in _ACTIONABLE_ROLES
                and _get_role(el) not in _ACTIONABLE_ROLES
            ):
                el = current
            try:
                _, parent = AXUIElementCopyAttributeValue(
                    current, "AXParent", None,
                )
            except Exception:
                break
            if parent is None:
                break
            current = parent

        # Extract final element info
        role_str = _get_role(el)
        label = _get_label(el)

        _, pos_val = AXUIElementCopyAttributeValue(el, "AXPosition", None)
        _, size_val = AXUIElementCopyAttributeValue(el, "AXSize", None)
        pos_x, pos_y = _extract_ax_point(pos_val)
        w, h = _extract_ax_size(size_val)
        if pos_x is None or pos_y is None or w is None or h is None:
            return None

        return ElementInfo(
            role=role_str,
            label=label,
            center_x=int(pos_x + w / 2),
            center_y=int(pos_y + h / 2),
            width=int(w),
            height=int(h),
        )

    def element_focused(self) -> ElementInfo | None:
        system = AXUIElementCreateSystemWide()
        err, el = AXUIElementCopyAttributeValue(system, "AXFocusedUIElement", None)
        if err != 0 or el is None:
            return None

        label = ""
        for attr in ("AXTitle", "AXDescription", "AXValue"):
            _, val = AXUIElementCopyAttributeValue(el, attr, None)
            if val and str(val).strip():
                label = str(val)
                break

        _, role = AXUIElementCopyAttributeValue(el, "AXRole", None)
        _, pos_val = AXUIElementCopyAttributeValue(el, "AXPosition", None)
        _, size_val = AXUIElementCopyAttributeValue(el, "AXSize", None)
        pos_x, pos_y = _extract_ax_point(pos_val)
        width, height = _extract_ax_size(size_val)
        if pos_x is None or pos_y is None or width is None or height is None:
            return None

        return ElementInfo(
            role=str(role) if role else "",
            label=label,
            center_x=int(pos_x + width / 2),
            center_y=int(pos_y + height / 2),
            width=int(width),
            height=int(height),
        )

    def find_elements(self, app: str, query: str) -> list:
        """Fuzzy-search UI elements by text — returns list of ElementInfo.

        Matches query (case-insensitive substring) against all text attributes.
        Dedupes by center position.
        """
        pid = self._find_app_pid(app)
        if pid is None:
            return []

        app_ref = AXUIElementCreateApplication(pid)

        # Force Chromium-based apps (Teams, Edge) to fully expose web content
        # AX tree. Without this, AXChildren on native View wrappers returns
        # empty, cutting off the entire web content subtree.
        try:
            AXUIElementSetAttributeValue(
                app_ref, "AXEnhancedUserInterface", True,
            )
        except Exception:
            pass

        _SKIP_ROLES = {
            "AXMenuBar", "AXMenu",
        }
        _LABEL_ATTRS = (
            "AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue",
        )

        query_lower = query.lower()
        results: list[ElementInfo] = []
        seen_positions: set[tuple[int, int]] = set()
        deadline = time.monotonic() + 3.0
        max_depth = 25

        stack: list[tuple[object, int]] = [(app_ref, 0)]
        while stack and len(results) < 50:
            if time.monotonic() > deadline:
                break
            el, depth = stack.pop()
            if depth > max_depth:
                continue

            err_r, role = AXUIElementCopyAttributeValue(el, "AXRole", None)
            role_str = str(role) if err_r == 0 and role else ""

            if role_str in _SKIP_ROLES:
                continue

            matched_label = ""
            for attr in _LABEL_ATTRS:
                try:
                    _, val = AXUIElementCopyAttributeValue(el, attr, None)
                except Exception:
                    continue
                if val and query_lower in str(val).lower():
                    matched_label = str(val)
                    break

            if matched_label:
                _, pos_val = AXUIElementCopyAttributeValue(
                    el, "AXPosition", None,
                )
                _, size_val = AXUIElementCopyAttributeValue(
                    el, "AXSize", None,
                )
                px, py = _extract_ax_point(pos_val)
                w, h = _extract_ax_size(size_val)
                if (
                    px is not None
                    and py is not None
                    and w is not None
                    and h is not None
                    and int(w) >= 5
                    and int(h) >= 5
                ):
                    cx, cy = int(px + w / 2), int(py + h / 2)
                    if (cx, cy) not in seen_positions:
                        seen_positions.add((cx, cy))
                        results.append(ElementInfo(
                            role=role_str,
                            label=matched_label,
                            center_x=cx,
                            center_y=cy,
                            width=int(w),
                            height=int(h),
                        ))

            try:
                err_c, children = AXUIElementCopyAttributeValue(
                    el, "AXChildren", None,
                )
            except Exception:
                continue
            if children:
                for i in range(len(children) - 1, -1, -1):
                    stack.append((children[i], depth + 1))

        if not results:
            log.warning("find_elements(%r, %r): 0 results (pid=%s)", app, query, pid)

        return results

    def activate_app(self, app: str) -> WindowInfo:
        """Bring an application to the foreground, launching it if needed."""
        running = self._find_running_app(app)
        if running is not None:
            pid = running.processIdentifier()
            bundle_id = running.bundleIdentifier() or ""
            if not bundle_id:
                raise RuntimeError(f"Application {app!r} has no bundle identifier")
            result = subprocess.run(
                ["open", "-b", bundle_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or f"exit code {result.returncode}"
                raise RuntimeError(f"Application {app!r} could not be activated: {detail}")
            return self._wait_for_app_window(app, pid=pid, bundle_id=bundle_id)

        app_path, bundle_id = self._resolve_application(app)
        result = subprocess.run(
            ["open", app_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"Application {app!r} could not be activated: {detail}")

        return self._wait_for_app_window(app, bundle_id=bundle_id)

    def _wait_for_app_window(
        self,
        app: str,
        *,
        pid: int | None = None,
        bundle_id: str = "",
    ) -> WindowInfo:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            window = self.get_active_window()
            if (
                window is not None
                and (pid is None or window.pid == pid)
                and (not bundle_id or window.bundle_id == bundle_id)
                and window.window_id
                and window.width > 0
                and window.height > 0
            ):
                return window
            time.sleep(0.05)
        actual = self.get_active_window()
        raise RuntimeError(
            f"Application {app!r} did not become active; active window is {actual}"
        )

    def _find_running_app(self, app: str):
        for item in NSWorkspace.sharedWorkspace().runningApplications():
            if app_identifier_matches(app, self._running_app_identifiers(item)):
                return item
        return None

    @staticmethod
    def _running_app_identifiers(item) -> tuple[str, ...]:
        if item is None:
            return ()
        identifiers = [item.localizedName() or "", item.bundleIdentifier() or ""]
        executable_url = item.executableURL()
        bundle_url = item.bundleURL()
        if executable_url is not None:
            executable = Path(executable_url.path())
            identifiers.extend((executable.stem, executable.name))
        if bundle_url is not None:
            bundle = Path(bundle_url.path())
            identifiers.extend((bundle.stem, bundle.name))
        return tuple(dict.fromkeys(identifier for identifier in identifiers if identifier))

    def _resolve_application(self, app: str) -> tuple[str, str]:
        workspace = NSWorkspace.sharedWorkspace()
        url = workspace.URLForApplicationWithBundleIdentifier_(app)
        path = url.path() if url is not None else workspace.fullPathForApplication_(app)
        if path is None:
            escaped = app.replace("\\", "\\\\").replace("'", "\\'")
            query = (
                "kMDItemContentType == 'com.apple.application-bundle' && "
                f"(kMDItemDisplayName == '{escaped}'c || "
                f"kMDItemFSName == '{escaped}.app'c)"
            )
            result = subprocess.run(
                ["mdfind", query], capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or f"exit code {result.returncode}"
                raise RuntimeError(f"Application {app!r} could not be resolved: {detail}")
            matches = [line for line in result.stdout.splitlines() if line]
            if len(matches) != 1:
                raise RuntimeError(
                    f"Application {app!r} resolved to {len(matches)} app bundles"
                )
            path = matches[0]

        bundle = NSBundle.bundleWithPath_(path)
        bundle_id = bundle.bundleIdentifier() if bundle is not None else ""
        if not bundle_id:
            raise RuntimeError(f"Application {app!r} has no bundle identifier")
        return path, bundle_id

    def _find_process_name(self, app_name: str) -> str | None:
        """Find the System Events process name for an app."""
        for app_info in NSWorkspace.sharedWorkspace().runningApplications():
            localized = app_info.localizedName() or ""
            if app_name.lower() in localized.lower():
                # System Events uses the bundle's executable name,
                # which is usually the localized name
                return localized
        return None

    def _find_app_pid(self, app_name: str) -> int | None:
        """Find PID of an app by name."""
        for app_info in NSWorkspace.sharedWorkspace().runningApplications():
            name = app_info.localizedName() or ""
            if name.lower() == app_name.lower() or app_name.lower() in name.lower():
                return app_info.processIdentifier()
        return None

    # ── Notifications ────────────────────────────────────

    def notify(self, title: str, message: str, *, sound: bool = True) -> None:
        script = (
            f'display notification "{_escape_applescript(message)}" '
            f'with title "{_escape_applescript(title)}"'
        )
        if sound:
            script += ' sound name "Glass"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)

    # ── Text prompt (Spotlight-style floating input) ────

    def prompt_text(
        self, title: str, placeholder: str = "", message: str = "",
    ) -> str | None:
        """Show a Spotlight-style floating NSPanel and wait for user input."""
        # Use a subprocess to avoid AppKit / pynput main-thread conflicts.
        # The subprocess creates an NSPanel, waits for Enter, prints JSON result.
        script = (
            _PROMPT_PANEL_SCRIPT
            .replace("__TITLE__", _escape_applescript(title))
            .replace("__PLACEHOLDER__", _escape_applescript(placeholder))
            .replace("__MESSAGE__", _escape_applescript(message))
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout.strip())
            return data.get("text")
        except Exception:
            return None

    # ── Global hotkey ────────────────────────────────────

    def register_hotkey(self, keys: list[str], callback: Callable[[], None]) -> Callable[[], None]:
        # Check accessibility permission — prompt the user if not granted
        self._ensure_accessibility()

        # Map key names to modifier flags and keycodes
        modifier_flags = 0
        trigger_vk: int | None = None

        flag_map = {
            "alt": kCGEventFlagMaskAlternate,
            "option": kCGEventFlagMaskAlternate,
            "shift": kCGEventFlagMaskShift,
            "cmd": kCGEventFlagMaskCommand,
            "ctrl": kCGEventFlagMaskControl,
        }
        vk_map = {
            "a": 0,
            "s": 1,
            "d": 2,
            "f": 3,
            "h": 4,
            "g": 5,
            "z": 6,
            "x": 7,
            "c": 8,
            "v": 9,
            "b": 11,
            "q": 12,
            "w": 13,
            "e": 14,
            "r": 15,
            "y": 16,
            "t": 17,
        }

        for k in keys:
            k_lower = k.lower()
            if k_lower in flag_map:
                modifier_flags |= flag_map[k_lower]
            elif k_lower in vk_map:
                trigger_vk = vk_map[k_lower]
            else:
                raise ValueError(f"Unknown key: {k}")

        if trigger_vk is None:
            raise ValueError(f"Hotkey must include a non-modifier key, got: {keys}")

        run_loop_ref = [None]

        def _tap_callback(_proxy, _type, event, _refcon):
            keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            flags = CGEventGetFlags(event)
            if keycode == trigger_vk and (flags & modifier_flags) == modifier_flags:
                callback()
            return event

        def _run_tap():
            tap = CGEventTapCreate(
                kCGSessionEventTap,
                kCGHeadInsertEventTap,
                0,  # listenOnly = 0 means we can observe
                CGEventMaskBit(kCGEventKeyDown),
                _tap_callback,
                None,
            )
            if tap is None:
                raise RuntimeError("Failed to create event tap. Check Accessibility permission.")

            source = CFMachPortCreateRunLoopSource(None, tap, 0)
            run_loop_ref[0] = CFRunLoopGetCurrent()
            CFRunLoopAddSource(run_loop_ref[0], source, kCFRunLoopCommonModes)
            CFRunLoopRun()

        thread = threading.Thread(target=_run_tap, daemon=True)
        thread.start()

        def unregister() -> None:
            if run_loop_ref[0]:
                CFRunLoopStop(run_loop_ref[0])

        return unregister

    def _ensure_accessibility(self) -> None:
        """Check Accessibility permission. If not granted, open the system prompt."""
        options = {
            "AXTrustedCheckOptionPrompt": kCFBooleanTrue,
        }
        trusted = AXIsProcessTrustedWithOptions(options)
        if not trusted:
            raise RuntimeError(
                "Accessibility permission required. "
                "Grant access in the dialog that just opened, then re-run."
            )


def configure_capture_proof_window(ns_window: object) -> None:
    """Make an NSWindow invisible to screen capture on macOS.

    Sets NSWindowSharingNone, ignoresMouseEvents, and canJoinAllSpaces.
    Caller is responsible for app-level settings (e.g. activation policy).
    """
    # Invisible to screencapture / CGWindowListCreateImage / mss
    ns_window.setSharingType_(AppKit.NSWindowSharingNone)  # type: ignore[union-attr]

    # Visible on all Spaces
    ns_window.setCollectionBehavior_(  # type: ignore[union-attr]
        ns_window.collectionBehavior()  # type: ignore[union-attr]
        | AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
    )


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


# Python script run in a subprocess to show a Spotlight-style NSPanel.
# Avoids AppKit / pynput main-thread conflicts in the daemon process.
_PROMPT_PANEL_SCRIPT = '''
import json, sys
from AppKit import (
    NSApplication, NSApp, NSPanel, NSTextField, NSFont, NSScreen,
    NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSObject, NSColor,
)
from Foundation import NSMakeRect, NSPoint

MESSAGE = "__MESSAGE__"

class Delegate(NSObject):
    result = None
    field = None

    def controlTextDidEndEditing_(self, notification):
        text = self.field.stringValue().strip()
        if text:
            self.result = text
            NSApp.stop_(None)

    def windowWillClose_(self, notification):
        NSApp.stop_(None)

app = NSApplication.sharedApplication()
app.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory

delegate = Delegate.alloc().init()

# Calculate panel height based on message presence
msg_height = 0
if MESSAGE:
    # Estimate lines: ~60 chars per line at font size 13 in 488px width
    line_count = max(1, len(MESSAGE) // 60 + MESSAGE.count("\\n") + 1)
    msg_height = min(line_count * 18 + 12, 200)  # cap at 200px

panel_height = 52 + msg_height

# Style mask: titled(1) | closable(2) | nonactivating(128) | HUD(8192)
panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(0, 0, 520, panel_height),
    1 | 2 | 128 | 8192,
    NSBackingStoreBuffered,
    False,
)
panel.setLevel_(NSFloatingWindowLevel)
panel.setMovableByWindowBackground_(True)
panel.setHidesOnDeactivate_(False)
panel.setDelegate_(delegate)
panel.setTitle_("__TITLE__")

# Message label (read-only, above the input field)
if MESSAGE:
    label = NSTextField.alloc().initWithFrame_(
        NSMakeRect(16, 48, 488, msg_height)
    )
    label.setStringValue_(MESSAGE)
    label.setFont_(NSFont.systemFontOfSize_(13))
    label.setTextColor_(NSColor.secondaryLabelColor())
    label.setEditable_(False)
    label.setBordered_(False)
    label.setDrawsBackground_(False)
    label.setSelectable_(False)
    panel.contentView().addSubview_(label)

field = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 10, 488, 32))
field.setPlaceholderString_("__PLACEHOLDER__")
field.setFont_(NSFont.systemFontOfSize_(16))
field.setDelegate_(delegate)
panel.contentView().addSubview_(field)
delegate.field = field

screen = NSScreen.mainScreen()
if screen:
    f = screen.visibleFrame()
    x = f.origin.x + f.size.width / 2 - 260
    y = f.origin.y + f.size.height / 2 + 100
    panel.setFrameOrigin_(NSPoint(x, y))

panel.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)
field.becomeFirstResponder()

app.run()

if delegate.result:
    print(json.dumps({"text": delegate.result}))
else:
    print(json.dumps({"text": None}))
'''

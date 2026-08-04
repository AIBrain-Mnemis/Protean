"""Windows platform backend.

Uses:
- pywin32 (win32gui, win32api, win32process) for window info & management
- ctypes + user32.dll SendInput for input simulation
- PIL.ImageGrab for screenshots (GDI BitBlt under the hood)
- uiautomation for UI Automation (accessibility tree)
- ffmpeg gdigrab for screen recording
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import json
import logging
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import win32api
import win32clipboard
import win32con
import win32gui
import win32process

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
    window_matches_app,
)

log = logging.getLogger(__name__)


def _is_session_locked() -> bool:
    """Best-effort check: is the current WinSta0 session locked?

    Uses WTSQuerySessionInformation(WTSSessionInfoEx).SessionFlags. Returns
    False on any error so the caller never has to handle exceptions from
    diagnostics. This is intentionally a private module helper used only to
    add a hint to capture failures.
    """
    try:
        wtsapi = ctypes.windll.LoadLibrary("wtsapi32.dll")

        class _WTSINFOEX_LEVEL1(ctypes.Structure):
            _fields_ = [
                ("SessionId", ctypes.wintypes.DWORD),
                ("SessionState", ctypes.c_int),
                ("SessionFlags", ctypes.c_long),
                ("WinStationName", ctypes.c_wchar * 33),
                ("UserName", ctypes.c_wchar * 21),
                ("DomainName", ctypes.c_wchar * 18),
                ("LogonTime", ctypes.c_longlong),
                ("ConnectTime", ctypes.c_longlong),
                ("DisconnectTime", ctypes.c_longlong),
                ("LastInputTime", ctypes.c_longlong),
                ("CurrentTime", ctypes.c_longlong),
                ("IncomingBytes", ctypes.wintypes.DWORD),
                ("OutgoingBytes", ctypes.wintypes.DWORD),
                ("IncomingFrames", ctypes.wintypes.DWORD),
                ("OutgoingFrames", ctypes.wintypes.DWORD),
                ("IncomingCompressedBytes", ctypes.wintypes.DWORD),
                ("OutgoingCompressedBytes", ctypes.wintypes.DWORD),
            ]

        class _WTSINFOEX_UNION(ctypes.Union):
            _fields_ = [("Level1", _WTSINFOEX_LEVEL1)]

        class _WTSINFOEX(ctypes.Structure):
            _fields_ = [("Level", ctypes.wintypes.DWORD), ("Data", _WTSINFOEX_UNION)]

        sid = ctypes.wintypes.DWORD()
        if not ctypes.windll.kernel32.ProcessIdToSessionId(
            ctypes.windll.kernel32.GetCurrentProcessId(),
            ctypes.byref(sid),
        ):
            return False
        ppBuffer = ctypes.c_void_p()
        pBytesReturned = ctypes.wintypes.DWORD()
        ok = wtsapi.WTSQuerySessionInformationW(
            0,  # WTS_CURRENT_SERVER_HANDLE
            sid.value,
            25,  # WTSSessionInfoEx
            ctypes.byref(ppBuffer),
            ctypes.byref(pBytesReturned),
        )
        if not ok or not ppBuffer.value:
            return False
        info = ctypes.cast(ppBuffer, ctypes.POINTER(_WTSINFOEX)).contents
        flags = info.Data.Level1.SessionFlags
        wtsapi.WTSFreeMemory(ppBuffer)
        # SessionFlags: 0 = LOCKED, 1 = UNLOCKED, -1 = UNKNOWN
        return flags == 0
    except Exception:
        return False

# ── DPI awareness ────────────────────────────────────────
# Must be called before any Win32 API that returns coordinates.
# Three-level fallback: Per-Monitor V2 → Per-Monitor V1 → System DPI.
_DPI_AWARENESS_LEVEL = "none"

try:
    # Best: Per-Monitor V2 (Win10 1607+). Correct coords on all monitors.
    _r = ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    if _r:
        _DPI_AWARENESS_LEVEL = "v2"
    else:
        raise OSError("SetProcessDpiAwarenessContext returned 0")
except Exception:
    try:
        # Good: Per-Monitor V1 (Win8.1+).
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _DPI_AWARENESS_LEVEL = "per_monitor"
    except Exception:
        try:
            # Basic: System DPI aware (Vista+). Only primary monitor DPI.
            ctypes.windll.user32.SetProcessDPIAware()
            _DPI_AWARENESS_LEVEL = "system"
        except Exception:
            pass

# ── ctypes structures for SendInput ──────────────────────

LONG = ctypes.c_long
DWORD = ctypes.wintypes.DWORD
WORD = ctypes.wintypes.WORD

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

# Virtual key codes
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21  # Page Up
VK_NEXT = 0x22   # Page Down
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_F5 = 0x74
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_F12 = 0x7B


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", DWORD),
        ("union", _INPUT_UNION),
    ]


def _send_input(*inputs: INPUT) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    ctypes.windll.user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _make_mouse_input(dx: int = 0, dy: int = 0, data: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi.dx = dx
    inp.union.mi.dy = dy
    inp.union.mi.mouseData = data
    inp.union.mi.dwFlags = flags
    return inp


def _make_key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = vk
    inp.union.ki.wScan = scan
    inp.union.ki.dwFlags = flags
    return inp


# ── Key name → VK code mapping ──────────────────────────

_MODIFIER_VK = {
    "ctrl": VK_CONTROL, "control": VK_CONTROL,
    "alt": VK_MENU, "option": VK_MENU,
    "shift": VK_SHIFT,
    "win": VK_LWIN, "cmd": VK_LWIN, "command": VK_LWIN, "super": VK_LWIN,
}

_SPECIAL_VK = {
    "return": VK_RETURN, "enter": VK_RETURN,
    "tab": VK_TAB,
    "escape": VK_ESCAPE, "esc": VK_ESCAPE,
    "backspace": VK_BACK, "delete": VK_DELETE,
    "space": VK_SPACE,
    "up": VK_UP, "down": VK_DOWN, "left": VK_LEFT, "right": VK_RIGHT,
    "home": VK_HOME, "end": VK_END,
    "pageup": VK_PRIOR, "pagedown": VK_NEXT,
    "f1": VK_F1, "f2": VK_F2, "f3": VK_F3, "f4": VK_F4,
    "f5": VK_F5, "f6": VK_F6, "f7": VK_F7, "f8": VK_F8,
    "f9": VK_F9, "f10": VK_F10, "f11": VK_F11, "f12": VK_F12,
}

# ── UIA ControlType → macOS AX role mapping ─────────────

_UIA_ACTIONABLE_TYPES = {
    "ButtonControl", "EditControl", "CheckBoxControl", "RadioButtonControl",
    "ComboBoxControl", "ListItemControl", "TabItemControl", "HyperlinkControl",
    "TextControl", "ImageControl", "MenuItemControl", "SliderControl",
    "DataItemControl", "TreeItemControl", "DocumentControl",
}
_UIA_SKIP_TYPES = {"MenuBarControl", "MenuControl"}

_UIA_ROLE_MAP = {
    "ButtonControl": "AXButton",
    "EditControl": "AXTextField",
    "CheckBoxControl": "AXCheckBox",
    "RadioButtonControl": "AXRadioButton",
    "ComboBoxControl": "AXComboBox",
    "ListItemControl": "AXStaticText",
    "TabItemControl": "AXTab",
    "HyperlinkControl": "AXLink",
    "TextControl": "AXStaticText",
    "ImageControl": "AXImage",
    "MenuItemControl": "AXMenuItem",
    "SliderControl": "AXSlider",
    "DataItemControl": "AXStaticText",
    "TreeItemControl": "AXStaticText",
    "DocumentControl": "AXTextArea",
    "WindowControl": "AXWindow",
    "GroupControl": "AXGroup",
    "ToolBarControl": "AXToolbar",
    "MenuBarControl": "AXMenuBar",
    "MenuBarItemControl": "AXMenuBarItem",
    "ScrollBarControl": "AXScrollBar",
    "ListControl": "AXList",
    "TreeControl": "AXOutline",
    "TableControl": "AXTable",
    "PaneControl": "AXGroup",
}


def _get_process_name(pid: int) -> str:
    """Get process executable name by PID using ctypes."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    MAX_PATH = 260
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(MAX_PATH)
        size = ctypes.wintypes.DWORD(MAX_PATH)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).stem
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _get_process_identifiers(pid: int) -> tuple[str, ...]:
    """Return stable executable identifiers for a Windows process."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ()
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.wintypes.DWORD(len(buf))
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
            h, 0, buf, ctypes.byref(size),
        ):
            return ()
        executable = Path(buf.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)

    identifiers = [executable.stem, executable.name]
    try:
        translations = win32api.GetFileVersionInfo(
            str(executable), "\\VarFileInfo\\Translation",
        )
        for language, codepage in translations:
            description = win32api.GetFileVersionInfo(
                str(executable),
                f"\\StringFileInfo\\{language:04x}{codepage:04x}\\FileDescription",
            )
            if description:
                identifiers.append(str(description))
    except Exception:
        pass
    return tuple(dict.fromkeys(identifiers))


def _escape_ps(text: str) -> str:
    """Escape text for PowerShell XML strings."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
        .replace("\n", "&#10;").replace("'", "&apos;")
    )


def configure_capture_proof_window(hwnd: int) -> None:
    """Make a window invisible to screen capture on Windows.

    Takes a native HWND. Sets WDA_EXCLUDEFROMCAPTURE, WS_EX_TRANSPARENT
    (click-through), WS_EX_TOOLWINDOW (no taskbar entry), and WS_EX_NOACTIVATE.
    """
    user32 = ctypes.windll.user32

    # Invisible to BitBlt / DXGI / mss / dxcam
    WDA_EXCLUDEFROMCAPTURE = 0x00000011
    WDA_MONITOR = 0x00000001
    if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        # Win10 < 2004: fall back to WDA_MONITOR (shows black rect in capture)
        user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR)

    # Extended styles: layered (for alpha) + no taskbar entry + no activation.
    # WS_EX_TRANSPARENT is deliberately NOT set: it would make the overlay
    # click-through, breaking the close button. Capture-proofing comes from
    # WDA_EXCLUDEFROMCAPTURE above, which is independent of click handling.
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000

    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


class WindowsPlatform(Platform):
    """Windows implementation of the Platform protocol."""

    def __init__(self) -> None:
        self._recording_process: subprocess.Popen | None = None
        self._recording_path: Path | None = None
        self._audio_process: subprocess.Popen | None = None
        self._monitor_rects: dict[int, tuple[int, int, int, int]] = {}
        # Probe UIA availability once so callers can surface a clear message
        # instead of silently producing element-less recordings.
        try:
            import uiautomation  # noqa: F401
            self._uia_unavailable_reason: str | None = None
        except Exception as e:
            self._uia_unavailable_reason = (
                f"uiautomation not importable ({type(e).__name__}: {e}) — "
                "element prefetch disabled. Run: uv sync"
            )

    @property
    def name(self) -> str:
        return "windows"

    # ── Window info ──────────────────────────────────────

    def get_active_window(self) -> WindowInfo | None:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None

        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process_name = _get_process_name(pid)

        # When title is empty (e.g. Edge Find bar overlay, popup child
        # windows), walk up to the root owner window for a title.
        if not title:
            GA_ROOTOWNER = 3
            root = ctypes.windll.user32.GetAncestor(hwnd, GA_ROOTOWNER)
            if root and root != hwnd:
                title = win32gui.GetWindowText(root)
            # Last resort: find any visible titled window of the same process
            if not title:
                def _find_titled(h: int, _: object) -> bool:
                    nonlocal title
                    try:
                        _, p = win32process.GetWindowThreadProcessId(h)
                        if p == pid and win32gui.IsWindowVisible(h):
                            t = win32gui.GetWindowText(h)
                            if t:
                                title = t
                                return False
                    except Exception:
                        pass
                    return True
                try:
                    win32gui.EnumWindows(_find_titled, None)
                except Exception:
                    pass

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            left = top = right = bottom = 0

        return WindowInfo(
            pid=pid,
            process_name=process_name,
            window_title=title,
            window_id=str(hwnd),
            x=left, y=top,
            width=right - left, height=bottom - top,
        )

    def get_window_at_point(self, x: int, y: int) -> WindowInfo | None:
        """Hit-test the screen at (x, y) and return its top-level window.

        Walks up to the root window so child controls map to their host frame.
        """
        try:
            hwnd = win32gui.WindowFromPoint((int(x), int(y)))
        except Exception:
            return None
        if not hwnd:
            return None

        GA_ROOT = 2
        try:
            root = ctypes.windll.user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        except Exception:
            root = hwnd

        try:
            title = win32gui.GetWindowText(root)
        except Exception:
            title = ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(root)
        except Exception:
            return None
        try:
            left, top, right, bottom = win32gui.GetWindowRect(root)
        except Exception:
            left = top = right = bottom = 0

        # Popups (context menus, IME emoji panel, Chromium overlays) often
        # hit-test to a top-level HWND whose GetWindowText is empty. Fall
        # back to the foreground window's title so events stay attributable.
        if not title:
            active = self.get_active_window()
            if active is not None and active.window_title:
                title = active.window_title

        return WindowInfo(
            pid=pid,
            process_name=_get_process_name(pid),
            window_title=title,
            window_id=str(root),
            x=left, y=top,
            width=right - left, height=bottom - top,
        )

    def list_windows(self) -> list[WindowInfo]:
        return self._list_activatable_windows()

    def _list_activatable_windows(self) -> list[WindowInfo]:
        results: list[WindowInfo] = []

        def _enum_cb(hwnd: int, _: object) -> bool:
            if not self._is_activatable_hwnd(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            except Exception:
                left = top = right = bottom = 0
            results.append(WindowInfo(
                pid=pid,
                process_name=_get_process_name(pid),
                window_title=title,
                window_id=str(hwnd),
                x=left, y=top,
                width=right - left, height=bottom - top,
                app_identifiers=_get_process_identifiers(pid),
            ))
            return True

        win32gui.EnumWindows(_enum_cb, None)
        return results

    @staticmethod
    def _is_activatable_hwnd(hwnd: int) -> bool:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return False
        get_shell_window = ctypes.windll.user32.GetShellWindow
        get_shell_window.argtypes = ()
        get_shell_window.restype = ctypes.wintypes.HWND
        if hwnd == get_shell_window():
            return False

        extended_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if extended_style & win32con.WS_EX_NOACTIVATE:
            return False
        if (
            extended_style & win32con.WS_EX_TOOLWINDOW
            and not extended_style & win32con.WS_EX_APPWINDOW
        ):
            return False
        if WindowsPlatform._is_cloaked_hwnd(hwnd):
            return False
        if extended_style & win32con.WS_EX_LAYERED:
            try:
                _, alpha, flags = win32gui.GetLayeredWindowAttributes(hwnd)
            except win32gui.error:
                pass
            else:
                if flags & win32con.LWA_ALPHA and alpha == 0:
                    return False

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return right - left >= 2 and bottom - top >= 2

    @staticmethod
    def _is_cloaked_hwnd(hwnd: int) -> bool:
        cloaked = ctypes.wintypes.DWORD()
        get_attribute = ctypes.windll.dwmapi.DwmGetWindowAttribute
        get_attribute.argtypes = (
            ctypes.wintypes.HWND,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
        )
        get_attribute.restype = ctypes.c_long
        result = get_attribute(
            ctypes.wintypes.HWND(hwnd),
            14,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return result == 0 and bool(cloaked.value)

    def activate_window(self, window_id: str) -> WindowInfo:
        try:
            target_hwnd = int(window_id)
        except ValueError as error:
            raise ValueError(f"Invalid Windows window ID: {window_id!r}") from error
        if not self._is_activatable_hwnd(target_hwnd):
            raise RuntimeError(f"Window {window_id!r} is not activatable")
        return self._activate_window_handle(
            target_hwnd, f"window {window_id!r}", exact=True,
        )

    def list_notifications(self) -> list[WindowInfo]:
        return []

    # ── Display info ─────────────────────────────────────

    def get_displays(self) -> list[DisplayInfo]:
        monitors = win32api.EnumDisplayMonitors(None, None)
        results: list[DisplayInfo] = []
        self._monitor_rects.clear()

        for idx, (hMonitor, _hdcMonitor, _rect) in enumerate(monitors, 1):
            info = win32api.GetMonitorInfo(hMonitor)
            mon_rect = info["Monitor"]  # (left, top, right, bottom)
            is_primary = bool(info.get("Flags", 0) & 1)  # MONITORINFOF_PRIMARY

            scale = 1.0
            try:
                dpi_x = ctypes.c_uint()
                dpi_y = ctypes.c_uint()
                ctypes.windll.shcore.GetDpiForMonitor(
                    hMonitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y),
                )
                scale = dpi_x.value / 96.0
            except Exception:
                pass

            self._monitor_rects[idx] = (
                mon_rect[0], mon_rect[1], mon_rect[2], mon_rect[3],
            )

            results.append(DisplayInfo(
                display_id=hMonitor if isinstance(hMonitor, int) else idx,
                display_index=idx,
                width=mon_rect[2] - mon_rect[0],
                height=mon_rect[3] - mon_rect[1],
                origin_x=mon_rect[0],
                origin_y=mon_rect[1],
                scale_factor=scale,
                is_primary=is_primary,
            ))

        return results

    def get_cursor_position(self) -> tuple[int, int]:
        # Prefer GetPhysicalCursorPos (always returns physical pixel coords)
        pt = ctypes.wintypes.POINT()
        try:
            if ctypes.windll.user32.GetPhysicalCursorPos(ctypes.byref(pt)):
                return (pt.x, pt.y)
        except Exception:
            pass
        return win32api.GetCursorPos()

    # ── Screen capture ───────────────────────────────────

    def capture_display(self, display_index: int, output_path: Path) -> None:
        """Capture a single display via PIL.ImageGrab (GDI BitBlt).

        Raises OSError on failure with a hint when the workstation is locked
        (the only common, recoverable cause of `screen grab failed` on Windows).
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fmt = "JPEG" if str(output_path).lower().endswith((".jpg", ".jpeg")) else "PNG"

        # Ensure monitor rects are populated
        if not self._monitor_rects:
            self.get_displays()
        rect = self._monitor_rects.get(display_index)

        from PIL import ImageGrab
        try:
            if rect is not None:
                img = ImageGrab.grab(bbox=rect, all_screens=True)
            else:
                img = ImageGrab.grab(all_screens=True)
        except OSError as e:
            hint = ""
            if _is_session_locked():
                hint = (
                    " — workstation appears to be locked; "
                    "Windows cannot capture a locked desktop"
                )
            raise OSError(
                f"capture_display failed for display {display_index}{hint}: {e}"
            ) from e
        img.save(output_path, fmt)

    # ── Power / session management ───────────────────────

    @contextlib.contextmanager
    def keep_awake(self) -> Iterator[None]:
        """Inhibit display sleep and idle-lock for the duration of the block.

        Uses SetThreadExecutionState with ES_CONTINUOUS, so a single call on
        enter holds the assertion until we explicitly clear it on exit. No
        per-step heartbeat is needed.

        Note: this defers the screensaver/idle-lock path. It does NOT defeat
        Group Policy `InactivityTimeoutSecs`, an explicit Win+L, or RDP
        session disconnect.
        """
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        prev = 0
        try:
            prev = ctypes.windll.kernel32.SetThreadExecutionState(flags)
            if prev == 0:
                log.warning("SetThreadExecutionState returned 0; keep-awake may be ineffective")
            else:
                log.debug("Keep-awake enabled (prev state=0x%x)", prev)
            yield
        finally:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                log.debug("Failed to clear ES_CONTINUOUS state", exc_info=True)

    # ── Screen recording ─────────────────────────────────

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

        # Get display geometry for offset
        displays = self.get_displays()
        target = None
        for d in displays:
            if d.display_index == display_index:
                target = d
                break
        if target is None and displays:
            target = displays[0]

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "gdigrab", "-framerate", "30",
        ]
        if target:
            cmd.extend([
                "-offset_x", str(target.origin_x),
                "-offset_y", str(target.origin_y),
                "-video_size", f"{target.width}x{target.height}",
            ])
        if show_clicks:
            cmd.extend(["-draw_mouse", "1"])
        cmd.extend([
            "-i", "desktop",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(output_path),
        ])

        self._recording_process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self._recording_path = output_path

        if capture_audio:
            audio_device = self._find_audio_device()
            if audio_device:
                audio_path = output_path.with_suffix(".audio.m4a")
                audio_cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "dshow", "-i", f"audio={audio_device}",
                    "-acodec", "aac", str(audio_path),
                ]
                self._audio_process = subprocess.Popen(
                    audio_cmd, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )

    def stop_screen_recording(self) -> Path | None:
        if self._recording_process is None:
            return None

        output_path = self._recording_path

        # Send 'q' to ffmpeg for graceful stop
        try:
            self._recording_process.stdin.write(b"q")
            self._recording_process.stdin.flush()
        except Exception:
            pass
        try:
            self._recording_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._recording_process.kill()
            self._recording_process.wait()
        self._recording_process = None

        if self._audio_process is not None:
            try:
                self._audio_process.stdin.write(b"q")
                self._audio_process.stdin.flush()
            except Exception:
                pass
            try:
                self._audio_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._audio_process.kill()
                self._audio_process.wait()
            self._audio_process = None

        if output_path is None or not output_path.exists() or output_path.stat().st_size == 0:
            self._recording_path = None
            return None

        # Merge audio if present
        audio_path = output_path.with_suffix(".audio.m4a")
        if audio_path.exists() and audio_path.stat().st_size > 0:
            merged = output_path.with_suffix(".merged" + output_path.suffix)
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(output_path), "-i", str(audio_path),
                    "-c:v", "copy", "-c:a", "aac", "-shortest", str(merged),
                ],
                capture_output=True,
            )
            if result.returncode == 0 and merged.exists():
                output_path.unlink(missing_ok=True)
                merged.rename(output_path)
            else:
                merged.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)

        self._recording_path = None
        return output_path

    def _find_audio_device(self) -> str | None:
        """Find a virtual audio device via ffmpeg dshow."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                capture_output=True, text=True, timeout=5,
            )
            for pattern in [
                "Microsoft Teams Audio", "ZoomAudioDevice", "WebEx",
                "Discord", "Slack", "CABLE Output", "VB-Cable",
            ]:
                if pattern in result.stderr:
                    return pattern
        except Exception:
            pass
        return None

    # ── Input simulation ─────────────────────────────────

    def click(
        self, x: int, y: int, button: MouseButton = "left", click_count: int = 1,
    ) -> None:
        ctypes.windll.user32.SetCursorPos(x, y)
        time.sleep(0.02)

        if button == "right":
            down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        elif button == "middle":
            down, up = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
        else:
            down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP

        for _ in range(click_count):
            _send_input(_make_mouse_input(flags=down), _make_mouse_input(flags=up))
            if click_count > 1:
                time.sleep(0.05)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        """Drag from one point to another via LEFTDOWN, cursor moves, LEFTUP.

        Moves the cursor through intermediate points (rather than
        teleporting) so apps that track drag position see a real
        gesture instead of a jump.
        """
        ctypes.windll.user32.SetCursorPos(from_x, from_y)
        time.sleep(0.02)
        _send_input(_make_mouse_input(flags=MOUSEEVENTF_LEFTDOWN))
        steps = 10
        for step in range(1, steps + 1):
            ix = from_x + (to_x - from_x) * step // steps
            iy = from_y + (to_y - from_y) * step // steps
            ctypes.windll.user32.SetCursorPos(ix, iy)
            time.sleep(0.01)
        _send_input(_make_mouse_input(flags=MOUSEEVENTF_LEFTUP))

    def scroll(
        self,
        x: int,
        y: int,
        direction: ScrollDirection = "down",
        amount: int = 3,
    ) -> None:
        self.move_cursor(x, y)
        time.sleep(0.02)
        if direction in ("up", "down"):
            data = amount * WHEEL_DELTA if direction == "up" else -amount * WHEEL_DELTA
            _send_input(_make_mouse_input(data=data, flags=MOUSEEVENTF_WHEEL))
        else:
            data = amount * WHEEL_DELTA if direction == "right" else -amount * WHEEL_DELTA
            _send_input(_make_mouse_input(data=data, flags=MOUSEEVENTF_HWHEEL))

    def move_cursor(self, x: int, y: int) -> None:
        ctypes.windll.user32.SetCursorPos(x, y)

    def type_text(self, text: str) -> None:
        """Type text via clipboard paste (Ctrl+V).

        Saves and restores the previous clipboard content so the user's
        clipboard is not clobbered.
        """
        # Save current clipboard text (best-effort; images/files are lost).
        old_text: str | None = None
        win32clipboard.OpenClipboard()
        try:
            old_text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except Exception:
            pass
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        win32clipboard.CloseClipboard()

        time.sleep(0.05)
        self.key_press("ctrl", "v")
        time.sleep(0.15)

        # Restore previous clipboard.
        if old_text is not None:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, old_text)
            win32clipboard.CloseClipboard()

    def get_clipboard(self) -> ClipboardContent:
        """Read structured clipboard content (text, files, or image metadata)."""
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            return ClipboardContent()
        try:
            # Files (CF_HDROP = 15)
            try:
                data = win32clipboard.GetClipboardData(win32con.CF_HDROP)
                if data:
                    return ClipboardContent(kind="files", files=list(data))
            except Exception:
                pass

            # Text (CF_UNICODETEXT = 13) — check before image because
            # Office apps put both CF_DIB and CF_UNICODETEXT on clipboard
            # when copying text; text is more informative.
            try:
                text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                if text:
                    return ClipboardContent(kind="text", text=text)
            except Exception:
                pass

            # Image (CF_DIB = 8) — extract dimensions from BITMAPINFOHEADER
            try:
                import struct

                dib = win32clipboard.GetClipboardData(win32con.CF_DIB)
                if dib and len(dib) >= 16:
                    _, w, h = struct.unpack_from("<Iii", dib, 0)
                    return ClipboardContent(
                        kind="image", image_width=abs(w), image_height=abs(h)
                    )
            except Exception:
                pass

            return ClipboardContent()
        finally:
            win32clipboard.CloseClipboard()

    def key_press(self, *keys: str) -> None:
        """Press a keyboard shortcut via SendInput.

        Examples: key_press("ctrl", "s"), key_press("return"), key_press("ctrl", "shift", "e")
        """
        modifiers_vk: list[int] = []
        trigger_vk: int | None = None
        trigger_char: str | None = None

        for k in keys:
            kl = k.lower()
            if kl in _MODIFIER_VK:
                modifiers_vk.append(_MODIFIER_VK[kl])
            elif kl in _SPECIAL_VK:
                trigger_vk = _SPECIAL_VK[kl]
            elif len(k) == 1:
                vk_result = ctypes.windll.user32.VkKeyScanW(ctypes.c_wchar(k))
                if vk_result != -1:
                    trigger_vk = vk_result & 0xFF
                    if (vk_result >> 8) & 1 and VK_SHIFT not in modifiers_vk:
                        modifiers_vk.append(VK_SHIFT)
                else:
                    trigger_char = k
            else:
                log.warning("Unknown key: %s", k)
                return

        inputs: list[INPUT] = []
        for vk in modifiers_vk:
            inputs.append(_make_key_input(vk=vk))

        if trigger_vk is not None:
            inputs.append(_make_key_input(vk=trigger_vk))
            inputs.append(_make_key_input(vk=trigger_vk, flags=KEYEVENTF_KEYUP))
        elif trigger_char is not None:
            code = ord(trigger_char)
            inputs.append(_make_key_input(scan=code, flags=KEYEVENTF_UNICODE))
            inputs.append(_make_key_input(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))

        for vk in reversed(modifiers_vk):
            inputs.append(_make_key_input(vk=vk, flags=KEYEVENTF_KEYUP))

        if inputs:
            _send_input(*inputs)

    # ── Accessibility (UI element discovery) ─────────────

    def _click_element_center(self, el: object) -> None:
        """Click on the center of a UIA element's bounding rectangle."""
        rect = el.BoundingRectangle
        cx = int(rect.left + rect.width() / 2)
        cy = int(rect.top + rect.height() / 2)
        self.click(cx, cy)

    def _find_app_window(self, app: str, timeout: float = 3.0):
        """Find a top-level UIA window for an app by name (fuzzy match).

        Uses Win32 EnumWindows (fast, never hangs) to find the window handle,
        then uiautomation.ControlFromHandle to get the UIA element for that
        specific window. Avoids uiautomation's tree traversal from root which
        can hang when any top-level window has an unresponsive UIA provider.
        """
        try:
            import uiautomation
        except ImportError:
            return None

        app_lower = app.lower()
        user32 = ctypes.windll.user32

        # Collect visible windows via Win32 (never hangs)
        candidates: list[tuple[int, str, str]] = []  # (hwnd, title, process_name)
        def _enum_cb(hwnd, _lParam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pname = _get_process_name(pid.value)
            candidates.append((hwnd, title, pname))
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM,
        )
        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)

        # Score candidates
        best_hwnd = None
        best_score = 0.0
        for hwnd, title, pname in candidates:
            if app_lower in title.lower():
                score = len(app) / len(title)
                if score > best_score:
                    best_score = score
                    best_hwnd = hwnd
            if app_lower in pname.lower() and 0.5 > best_score:
                best_score = 0.5
                best_hwnd = hwnd

        if best_hwnd is None:
            return None

        try:
            return uiautomation.ControlFromHandle(best_hwnd)
        except Exception as e:
            log.debug("ControlFromHandle failed: %s", e)
            return None

    def _find_uia_element(
        self, app: str, label: str, *, role: str = "", timeout: float = 3.0,
    ) -> object | None:
        """DFS search for a UIA element matching label.

        Mirrors macOS _find_ax_element: role-based pruning + time budget.
        """
        try:
            import uiautomation  # noqa: F401
        except ImportError:
            return None

        win = self._find_app_window(app, timeout=timeout)
        if win is None:
            return None

        max_depth = 30
        deadline = time.monotonic() + timeout
        label_lower = label.lower()

        stack: list[tuple[object, int]] = [(win, 0)]

        while stack:
            if time.monotonic() > deadline:
                break
            el, depth = stack.pop()
            if depth > max_depth:
                continue

            try:
                control_type = el.ControlTypeName or ""
            except Exception:
                continue

            if control_type in _UIA_SKIP_TYPES:
                continue

            if control_type in _UIA_ACTIONABLE_TYPES:
                for getter in [
                    lambda: el.Name or "",
                    lambda: el.AutomationId or "",
                    lambda: getattr(el, "HelpText", "") or "",
                ]:
                    try:
                        val = getter()
                    except Exception:
                        continue
                    if val and label_lower in val.lower():
                        if role:
                            mapped = _UIA_ROLE_MAP.get(control_type, control_type)
                            if role != control_type and role != mapped:
                                continue
                        return el

            try:
                children = el.GetChildren()
            except Exception:
                continue
            if children:
                for i in range(len(children) - 1, -1, -1):
                    stack.append((children[i], depth + 1))

        return None

    def find_element(
        self, app: str, label: str, *, role: str = "", timeout: float = 3.0,
    ) -> tuple[int, int] | None:
        el = self._find_uia_element(app, label, role=role, timeout=timeout)
        if el is None:
            return None
        try:
            rect = el.BoundingRectangle
            if rect.width() <= 0 or rect.height() <= 0:
                return None
            return (int(rect.left + rect.width() / 2), int(rect.top + rect.height() / 2))
        except Exception:
            return None

    def ax_press(self, app: str, label: str, *, role: str = "") -> bool:
        """Find element and invoke it (UIA equivalent of macOS AXPress)."""
        el = self._find_uia_element(app, label, role=role)
        if el is None:
            return False

        # Try InvokePattern (buttons, links, menu items)
        try:
            inv = el.GetInvokePattern()
            if inv:
                inv.Invoke()
                return True
        except Exception:
            pass

        # Try TogglePattern (checkboxes)
        try:
            tog = el.GetTogglePattern()
            if tog:
                tog.Toggle()
                return True
        except Exception:
            pass

        # Try ExpandCollapsePattern (dropdowns)
        try:
            exp = el.GetExpandCollapsePattern()
            if exp:
                exp.Expand()
                return True
        except Exception:
            pass

        # Last resort: click on element center
        try:
            rect = el.BoundingRectangle
            self.click(int(rect.left + rect.width() / 2), int(rect.top + rect.height() / 2))
            return True
        except Exception:
            return False

    def select_option(self, app: str, label: str, value: str) -> bool:
        """Select an option from a dropdown/combobox."""
        try:
            import uiautomation  # noqa: F401
        except ImportError:
            return False

        el = self._find_uia_element(app, label, role="ComboBoxControl")
        if el is None:
            el = self._find_uia_element(app, label)
        if el is None:
            return False

        value_lower = value.lower()

        # Strategy 1: ExpandCollapse → find item → SelectionItem/Invoke/click
        try:
            exp = el.GetExpandCollapsePattern()
            if exp:
                exp.Expand()
                time.sleep(0.3)

                deadline = time.monotonic() + 3.0
                stack = [(el, 0)]
                while stack:
                    if time.monotonic() > deadline:
                        break
                    node, depth = stack.pop()
                    if depth > 15:
                        continue
                    try:
                        name = node.Name or ""
                    except Exception:
                        name = ""
                    if name and value_lower in name.lower():
                        for action in [
                            lambda: node.GetSelectionItemPattern().Select(),
                            lambda: node.GetInvokePattern().Invoke(),
                            lambda: self._click_element_center(node),
                        ]:
                            try:
                                action()
                                time.sleep(0.2)
                                return True
                            except Exception:
                                continue
                    try:
                        for child in reversed(node.GetChildren() or []):
                            stack.append((child, depth + 1))
                    except Exception:
                        continue

                try:
                    exp.Collapse()
                except Exception:
                    self.key_press("escape")
                return False
        except Exception:
            pass

        # Strategy 2: click to open → search → click match
        try:
            rect = el.BoundingRectangle
            self.click(int(rect.left + rect.width() / 2), int(rect.top + rect.height() / 2))
            time.sleep(0.5)
        except Exception:
            return False

        match = self._find_uia_element(app, value, timeout=2.0)
        if match:
            try:
                rect = match.BoundingRectangle
                self.click(int(rect.left + rect.width() / 2), int(rect.top + rect.height() / 2))
                return True
            except Exception:
                pass

        self.key_press("escape")
        return False

    def find_menu_item(self, app: str, menu_path: str) -> bool:
        """Click a menu item by path (e.g. "File > Save")."""
        parts = [p.strip() for p in menu_path.split(">")]
        if len(parts) < 2:
            return False

        self.activate_app(app)
        time.sleep(0.3)

        try:
            import uiautomation  # noqa: F401
        except ImportError:
            return False

        win = self._find_app_window(app, timeout=2.0)
        if win is None:
            return False

        current = win
        for part in parts:
            part_lower = part.lower()
            found = None
            deadline = time.monotonic() + 2.0
            stack = [(current, 0)]

            while stack:
                if time.monotonic() > deadline:
                    break
                el, depth = stack.pop()
                if depth > 5:
                    continue
                try:
                    ct = el.ControlTypeName or ""
                except Exception:
                    continue
                if ct in ("MenuBarItemControl", "MenuItemControl"):
                    try:
                        name = el.Name or ""
                        if name and part_lower in name.lower():
                            found = el
                            break
                    except Exception:
                        pass
                try:
                    for child in (el.GetChildren() or []):
                        stack.append((child, depth + 1))
                except Exception:
                    continue

            if found is None:
                return False

            for action in [
                lambda: found.GetInvokePattern().Invoke(),
                lambda: found.GetExpandCollapsePattern().Expand(),
                lambda: self._click_element_center(found),
            ]:
                try:
                    action()
                    time.sleep(0.3)
                    current = found
                    break
                except Exception:
                    continue
            else:
                return False

        return True

    def list_menu_items(self, app: str, menu_path: str = "") -> list[str]:
        try:
            import uiautomation  # noqa: F401
        except ImportError:
            return []

        win = self._find_app_window(app, timeout=2.0)
        if win is None:
            return []

        # Find menu bar
        menu_bar = None
        try:
            for child in win.GetChildren():
                try:
                    if child.ControlTypeName == "MenuBarControl":
                        menu_bar = child
                        break
                except Exception:
                    continue
        except Exception:
            return []

        if menu_bar is None:
            return []

        if not menu_path:
            try:
                return [
                    item.Name for item in menu_bar.GetChildren()
                    if item.Name and item.ControlTypeName == "MenuBarItemControl"
                ]
            except Exception:
                return []

        # Navigate into submenu
        parts = [p.strip() for p in menu_path.split(">")]
        current = menu_bar
        for part in parts:
            part_lower = part.lower()
            found = None
            try:
                for child in current.GetChildren():
                    try:
                        ct = child.ControlTypeName or ""
                        name = child.Name or ""
                        _MENU_TYPES = ("MenuBarItemControl", "MenuItemControl")
                        if ct in _MENU_TYPES and part_lower in name.lower():
                            found = child
                            break
                    except Exception:
                        continue
            except Exception:
                return []

            if found is None:
                return []

            for action in [
                lambda: found.GetExpandCollapsePattern().Expand(),
                lambda: found.GetInvokePattern().Invoke(),
                lambda: self.click(
                    int(found.BoundingRectangle.left + found.BoundingRectangle.width() / 2),
                    int(found.BoundingRectangle.top + found.BoundingRectangle.height() / 2),
                ),
            ]:
                try:
                    action()
                    time.sleep(0.3)
                    break
                except Exception:
                    continue
            current = found

        # Collect items
        try:
            result = []
            for child in current.GetChildren():
                try:
                    ct = child.ControlTypeName or ""
                    if ct == "MenuItemControl" and child.Name:
                        result.append(child.Name)
                    elif ct == "MenuControl":
                        for sub in child.GetChildren():
                            if sub.ControlTypeName == "MenuItemControl" and sub.Name:
                                result.append(sub.Name)
                except Exception:
                    continue
            self.key_press("escape")
            return result
        except Exception:
            self.key_press("escape")
            return []

    def list_elements(self, app: str, max_depth: int = 8) -> list[str]:
        try:
            import uiautomation  # noqa: F401
        except ImportError:
            return []

        win = self._find_app_window(app, timeout=2.0)
        if win is None:
            return []

        max_depth = min(max_depth, 15)
        results: list[str] = []
        deadline = time.monotonic() + 3.0

        stack: list[tuple[object, int]] = [(win, 0)]
        while stack and len(results) < 80:
            if time.monotonic() > deadline:
                break
            el, depth = stack.pop()
            if depth > max_depth:
                continue

            try:
                ct = el.ControlTypeName or ""
            except Exception:
                continue
            if ct in _UIA_SKIP_TYPES:
                continue
            if ct in _UIA_ACTIONABLE_TYPES:
                try:
                    name = el.Name or ""
                    auto_id = el.AutomationId or ""
                except Exception:
                    name = auto_id = ""
                display = name or auto_id
                if display:
                    indent = "  " * depth
                    ax_role = _UIA_ROLE_MAP.get(ct, ct)
                    results.append(f"{indent}{ax_role}: {display!r}")

            try:
                children = el.GetChildren()
            except Exception:
                continue
            if children:
                for i in range(len(children) - 1, -1, -1):
                    stack.append((children[i], depth + 1))

        return results

    def find_elements(self, app: str, query: str) -> list[ElementInfo]:
        try:
            import uiautomation  # noqa: F401
        except ImportError:
            return []

        win = self._find_app_window(app, timeout=2.0)
        if win is None:
            return []

        query_lower = query.lower()
        results: list[ElementInfo] = []
        seen: set[tuple[int, int]] = set()
        deadline = time.monotonic() + 3.0

        stack: list[tuple[object, int]] = [(win, 0)]
        while stack and len(results) < 50:
            if time.monotonic() > deadline:
                break
            el, depth = stack.pop()
            if depth > 25:
                continue

            try:
                ct = el.ControlTypeName or ""
            except Exception:
                continue
            if ct in _UIA_SKIP_TYPES:
                continue

            matched = ""
            for getter in [
                lambda: el.Name or "",
                lambda: el.AutomationId or "",
                lambda: getattr(el, "HelpText", "") or "",
            ]:
                try:
                    val = getter()
                except Exception:
                    continue
                if val and query_lower in val.lower():
                    matched = val
                    break

            if matched:
                try:
                    rect = el.BoundingRectangle
                    w, h = int(rect.width()), int(rect.height())
                    if w >= 5 and h >= 5:
                        cx = int(rect.left + w / 2)
                        cy = int(rect.top + h / 2)
                        if (cx, cy) not in seen:
                            seen.add((cx, cy))
                            results.append(ElementInfo(
                                role=_UIA_ROLE_MAP.get(ct, ct),
                                label=matched,
                                center_x=cx, center_y=cy,
                                width=w, height=h,
                            ))
                except Exception:
                    pass

            try:
                children = el.GetChildren()
            except Exception:
                continue
            if children:
                for i in range(len(children) - 1, -1, -1):
                    stack.append((children[i], depth + 1))

        if not results:
            log.warning("find_elements(%r, %r): 0 results", app, query)
        return results

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
        try:
            import uiautomation  # noqa: F401
        except ImportError as e:
            return AccessibilitySnapshot(
                unavailable_reason=f"uiautomation not importable: {e}",
            )

        target_app = app.strip()
        active = self.get_active_window()
        if not target_app:
            if active is None:
                return AccessibilitySnapshot(unavailable_reason="no active window")
            target_app = active.process_name or active.window_title
        win = self._find_app_window(target_app, timeout=timeout)
        if win is None:
            return AccessibilitySnapshot(
                app=target_app,
                unavailable_reason=f"application window not found: {target_app}",
            )

        query_lower = query.lower().strip()
        deadline = time.monotonic() + timeout
        max_depth = 25
        nodes: list[AccessibilityNode] = []
        seen: set[tuple[str, str, int, int, int, int]] = set()
        node_index = 1
        visited = 0
        truncated = False
        default_types = {
            "ButtonControl", "EditControl", "CheckBoxControl", "RadioButtonControl",
            "ComboBoxControl", "ListItemControl", "TabItemControl", "HyperlinkControl",
            "MenuItemControl", "SliderControl", "DocumentControl",
        }
        stack: list[tuple[object, int]] = [(win, 0)]

        while stack:
            if time.monotonic() > deadline:
                truncated = True
                break
            visited += 1
            if visited > max_visited:
                truncated = True
                break
            el, depth = stack.pop()
            if depth > max_depth:
                truncated = True
                continue

            try:
                control_type = el.ControlTypeName or ""
            except Exception:
                continue
            if control_type in _UIA_SKIP_TYPES:
                continue

            rect: Rect | None = None
            try:
                raw_rect = el.BoundingRectangle
                width = int(raw_rect.width())
                height = int(raw_rect.height())
                if width >= 5 and height >= 5:
                    rect = Rect(int(raw_rect.left), int(raw_rect.top), width, height)
            except Exception:
                rect = None

            visible = visible_bounds is None or rect is None or rect.intersects(visible_bounds)

            name = ""
            automation_id = ""
            help_text = ""
            query_hit = False
            for getter_name, getter in (
                ("name", lambda: el.Name or ""),
                ("automation_id", lambda: el.AutomationId or ""),
                ("help_text", lambda: getattr(el, "HelpText", "") or ""),
            ):
                try:
                    text = str(getter()).strip()[:120]
                except Exception:
                    text = ""
                if getter_name == "name":
                    name = text
                elif getter_name == "automation_id":
                    automation_id = text
                else:
                    help_text = text
                if query_lower and getter_name != "automation_id" and query_lower in text.lower():
                    query_hit = True
                    break

            role = _UIA_ROLE_MAP.get(control_type, control_type)
            query_matches = not query_lower or query_hit or query_lower in role.lower()

            states: list[str] = []
            try:
                if bool(getattr(el, "IsEnabled", False)):
                    states.append("enabled")
            except Exception:
                pass
            try:
                if bool(getattr(el, "HasKeyboardFocus", False)):
                    states.append("focused")
            except Exception:
                pass

            include_default = control_type in default_types or "focused" in states
            label = name or automation_id or help_text
            if (
                query_matches
                and visible
                and (query_lower or include_default)
                and rect is not None
                and label
            ):
                key = (role, label, rect.x, rect.y, rect.width, rect.height)
                if key not in seen:
                    actions: list[str] = []
                    if control_type in {
                        "ButtonControl", "CheckBoxControl", "RadioButtonControl",
                        "ComboBoxControl", "ListItemControl", "TabItemControl",
                        "HyperlinkControl", "MenuItemControl",
                    }:
                        actions.append("press")
                    if control_type in {"EditControl", "ComboBoxControl", "DocumentControl"}:
                        actions.extend(["focus", "set_text"])

                    seen.add(key)
                    nodes.append(AccessibilityNode(
                        id=str(node_index),
                        role=role,
                        raw_role=control_type,
                        label=name,
                        value=automation_id,
                        description=help_text,
                        x=rect.x,
                        y=rect.y,
                        width=rect.width,
                        height=rect.height,
                        depth=depth,
                        states=tuple(states),
                        actions=tuple(actions),
                    ))
                    node_index += 1
                    if len(nodes) >= max_nodes:
                        truncated = bool(stack)
                        break

            try:
                children = el.GetChildren()
            except Exception:
                continue
            if children:
                for i in range(len(children) - 1, -1, -1):
                    stack.append((children[i], depth + 1))

        window_title = ""
        try:
            window_title = win.Name or ""
        except Exception:
            pass
        return AccessibilitySnapshot(
            app=target_app,
            window_title=window_title,
            nodes=nodes,
            truncated=truncated,
        )

    def get_element_role(self, app: str, label: str) -> str | None:
        el = self._find_uia_element(app, label)
        if el is None:
            return None
        try:
            ct = el.ControlTypeName or ""
            return _UIA_ROLE_MAP.get(ct, ct) or None
        except Exception:
            return None

    @staticmethod
    def _drill_into_container(el: object, x: int, y: int) -> object:
        """If *el* is a container, find a more specific child at (x, y)."""
        _CONTAINERS = {"PaneControl", "GroupControl", "ListControl"}
        try:
            ct = el.ControlTypeName or ""
        except Exception:
            return el
        if ct not in _CONTAINERS:
            return el

        best = el
        try:
            children = el.GetChildren()
            for child in children or []:
                try:
                    r = child.BoundingRectangle
                    if not (r.left <= x <= r.right and r.top <= y <= r.bottom):
                        continue
                except Exception:
                    continue
                cnm = ""
                try:
                    cnm = child.Name or ""
                except Exception:
                    pass
                if cnm:
                    best = child
                # Try one more level for even more specific elements
                try:
                    grandchildren = child.GetChildren()
                    for gc in grandchildren or []:
                        try:
                            gr = gc.BoundingRectangle
                            if not (gr.left <= x <= gr.right and gr.top <= y <= gr.bottom):
                                continue
                        except Exception:
                            continue
                        try:
                            gnm = gc.Name or ""
                        except Exception:
                            gnm = ""
                        if gnm:
                            best = gc
                            break
                except Exception:
                    pass
                break  # first hit-testing child is enough
        except Exception:
            pass
        return best

    @staticmethod
    def _find_child_text(el: object) -> str:
        """Search immediate children for a TextControl/StaticText with a label."""
        try:
            for child in el.GetChildren() or []:
                try:
                    ct = child.ControlTypeName or ""
                except Exception:
                    continue
                if ct in ("TextControl", "StaticTextControl"):
                    try:
                        nm = child.Name or ""
                    except Exception:
                        nm = ""
                    if nm:
                        return nm
        except Exception:
            pass
        return ""

    def element_at(self, x: int, y: int) -> ElementInfo | None:
        """Get the accessibility element at screen coordinates."""
        try:
            import uiautomation
        except ImportError:
            return None

        try:
            el = uiautomation.ControlFromPoint(x, y)
        except Exception:
            return None
        if el is None:
            return None

        # When ControlFromPoint returns a container (PaneControl,
        # GroupControl, ListControl), drill into children up to 2
        # levels to find the specific element at (x, y).
        el = self._drill_into_container(el, x, y)

        # Walk up to nearest actionable ancestor with a label
        current = el
        for _ in range(15):
            try:
                ct = current.ControlTypeName or ""
            except Exception:
                break
            if ct in ("WindowControl", "PaneControl"):
                break
            try:
                name = current.Name or ""
            except Exception:
                name = ""
            if ct in _UIA_ACTIONABLE_TYPES and name:
                el = current
                break
            try:
                parent = current.GetParentControl()
            except Exception:
                break
            if parent is None:
                break
            current = parent

        return self._uia_to_element_info(el)

    def element_focused(self) -> ElementInfo | None:
        """Get the accessibility element that currently has keyboard focus."""
        try:
            import uiautomation
        except ImportError:
            return None
        try:
            el = uiautomation.GetFocusedControl()
        except Exception:
            return None
        if el is None:
            return None
        return self._uia_to_element_info(el)

    def _uia_to_element_info(self, el: object) -> ElementInfo | None:
        """Convert a uiautomation control into ElementInfo.

        Shared by element_at and element_focused: applies the ImageControl
        parent-name fallback, AvalonDock label sanitization, and the
        UIA-ControlType → AX-role mapping.
        """
        try:
            rect = el.BoundingRectangle
            ct = el.ControlTypeName or ""
            name = el.Name or ""

            # For generic container elements (e.g. PPT ImageControl whose
            # Name is a placeholder like "Title TextBox"), the parent often
            # carries a more descriptive Name (e.g. "Slide 6 - MSA").
            if ct == "ImageControl" and name:
                try:
                    parent = el.GetParentControl()
                    if parent:
                        pname = parent.Name or ""
                        if pname and pname != name:
                            name = pname
                except Exception:
                    pass

            # WPF docking frameworks (e.g. Xceed AvalonDock) expose internal
            # .NET class names as UIA Name (e.g. "Xceed.Wpf.AvalonDock.Layout.LayoutDocument").
            # Detect and replace with child text or discard.
            if name and "." in name and " " not in name and name.count(".") >= 2:
                child_label = self._find_child_text(el)
                name = child_label or ""

            return ElementInfo(
                role=_UIA_ROLE_MAP.get(ct, ct),
                label=name,
                center_x=int(rect.left + rect.width() / 2),
                center_y=int(rect.top + rect.height() / 2),
                width=int(rect.width()),
                height=int(rect.height()),
            )
        except Exception:
            return None

    def activate_app(self, app: str) -> WindowInfo:
        """Bring an app to foreground using AttachThreadInput trick."""
        target_hwnd = next(
            (
                int(window.window_id)
                for window in self._list_activatable_windows()
                if window_matches_app(window, app)
            ),
            None,
        )

        if target_hwnd is None:
            raise RuntimeError(f"Application window not found: {app!r}")

        return self._activate_window_handle(
            target_hwnd, f"application {app!r}", exact=True,
        )

    def _activate_window_handle(
        self, target_hwnd: int, target: str, *, exact: bool = False,
    ) -> WindowInfo:
        try:
            if win32gui.IsIconic(target_hwnd):
                win32gui.ShowWindow(target_hwnd, win32con.SW_RESTORE)

            fg_hwnd = win32gui.GetForegroundWindow()
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            tgt_tid, _ = win32process.GetWindowThreadProcessId(target_hwnd)
            fg_tid, _ = win32process.GetWindowThreadProcessId(fg_hwnd)

            ctypes.windll.user32.AllowSetForegroundWindow(-1)

            att_fg = att_tgt = False
            try:
                if cur_tid != fg_tid:
                    att_fg = bool(win32process.AttachThreadInput(cur_tid, fg_tid, True))
                if cur_tid != tgt_tid:
                    att_tgt = bool(win32process.AttachThreadInput(cur_tid, tgt_tid, True))
                win32gui.SetForegroundWindow(target_hwnd)
                win32gui.BringWindowToTop(target_hwnd)
                win32gui.SetWindowPos(
                    target_hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
            finally:
                if att_fg:
                    try:
                        win32process.AttachThreadInput(cur_tid, fg_tid, False)
                    except Exception:
                        pass
                if att_tgt:
                    try:
                        win32process.AttachThreadInput(cur_tid, tgt_tid, False)
                    except Exception:
                        pass
        except Exception as e:
            raise RuntimeError(f"{target.capitalize()} could not be activated: {e}") from e

        return self._wait_for_foreground_window(target, target_hwnd, exact=exact)

    def _wait_for_foreground_window(
        self, app: str, target_hwnd: int, *, exact: bool = False,
    ) -> WindowInfo:
        user32 = ctypes.windll.user32
        target_pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(target_hwnd, ctypes.byref(target_pid))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            foreground = user32.GetForegroundWindow()
            foreground_pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
            if (
                foreground == target_hwnd
                if exact
                else foreground_pid.value == target_pid.value
            ):
                window = self.get_active_window()
                if window is None:
                    raise RuntimeError(f"Application {app!r} has no active window")
                return window
            time.sleep(0.05)
        actual = self.get_active_window()
        raise RuntimeError(
            f"Application {app!r} did not become active; active window is {actual}"
        )

    # ── Notifications ────────────────────────────────────

    def notify(self, title: str, message: str, *, sound: bool = True) -> None:
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, "
            "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
            "[Windows.Data.Xml.Dom.XmlDocument, "
            "Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
            f'$xml = "<toast><visual><binding template=\\"ToastText02\\">'
            f'<text id=\\"1\\">{_escape_ps(title)}</text>'
            f'<text id=\\"2\\">{_escape_ps(message)}</text>'
            f"</binding></visual>"
        )
        if sound:
            ps_script += '<audio src="ms-winsoundevent:Notification.Default"/>'
        ps_script += (
            '</toast>"; '
            "$xd = New-Object Windows.Data.Xml.Dom.XmlDocument; "
            "$xd.LoadXml($xml); "
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xd); "
            "[Windows.UI.Notifications.ToastNotificationManager]::"
            'CreateToastNotifier("Protean").Show($toast)'
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, timeout=10,
            )
        except Exception as e:
            log.debug("Toast notification failed: %s", e)

    # ── Text prompt ──────────────────────────────────────

    def prompt_text(
        self, title: str, placeholder: str = "", message: str = "",
    ) -> str | None:
        """Show a WinForms input dialog via PowerShell subprocess."""
        ps_script = _PROMPT_PS_SCRIPT.replace(
            "__TITLE__", _escape_ps(title)
        ).replace(
            "__PLACEHOLDER__", _escape_ps(placeholder)
        ).replace(
            "__MESSAGE__", _escape_ps(message)
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout.strip())
            return data.get("text")
        except Exception:
            return None

    # ── Global hotkey ────────────────────────────────────

    def register_hotkey(
        self, keys: list[str], callback: Callable[[], None],
    ) -> Callable[[], None]:
        """Register a global hotkey using pynput (already a project dependency)."""
        from pynput import keyboard

        # Use non-lateralized keys (Key.ctrl, not Key.ctrl_l) because
        # listener.canonical() normalizes left/right variants.
        combo: set = set()
        for k in keys:
            kl = k.lower()
            if kl in ("ctrl", "control"):
                combo.add(keyboard.Key.ctrl)
            elif kl in ("alt", "option"):
                combo.add(keyboard.Key.alt)
            elif kl in ("shift",):
                combo.add(keyboard.Key.shift)
            elif kl in ("win", "cmd", "command", "super"):
                combo.add(keyboard.Key.cmd)
            else:
                try:
                    combo.add(keyboard.Key[kl])
                except KeyError:
                    if len(k) == 1:
                        combo.add(keyboard.KeyCode.from_char(k.lower()))
                    else:
                        raise ValueError(f"Unknown key: {k}")

        hotkey = keyboard.HotKey(frozenset(combo), callback)

        def on_press(key):
            hotkey.press(listener.canonical(key))

        def on_release(key):
            hotkey.release(listener.canonical(key))

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()

        return lambda: listener.stop()

    # ── Virtual audio ────────────────────────────────────

    # Removed: get_virtual_audio_device. The legacy call-routing helper was
    # retired when the daemon moved to the Electron bridge for realtime + call
    # transport.


# PowerShell script for WinForms input dialog (runs in subprocess).
_PROMPT_PS_SCRIPT = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$message = "__MESSAGE__"
$msgHeight = 0
if ($message) {
    $lines = $message.Split("`n").Count
    $charLines = [Math]::Ceiling($message.Length / 70)
    $lineCount = [Math]::Max(1, $charLines + $lines - 1)
    $msgHeight = [Math]::Min($lineCount * 20 + 10, 200)
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "__TITLE__"
$form.Size = New-Object System.Drawing.Size(540, (100 + $msgHeight))
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false

if ($message) {
    $label = New-Object System.Windows.Forms.Label
    $label.Location = New-Object System.Drawing.Point(10, 10)
    $label.Size = New-Object System.Drawing.Size(500, $msgHeight)
    $label.Text = $message
    $label.Font = New-Object System.Drawing.Font("Segoe UI", 10)
    $label.ForeColor = [System.Drawing.SystemColors]::GrayText
    $form.Controls.Add($label)
}

$textBox = New-Object System.Windows.Forms.TextBox
$textBox.Location = New-Object System.Drawing.Point(10, (20 + $msgHeight))
$textBox.Size = New-Object System.Drawing.Size(500, 30)
$textBox.Font = New-Object System.Drawing.Font("Segoe UI", 14)
$textBox.Text = "__PLACEHOLDER__"

$result = @{ text = $null }

$textBox.Add_KeyDown({
    if ($_.KeyCode -eq "Return") {
        $result.text = $textBox.Text
        $form.Close()
    }
})

$form.Add_Shown({ $textBox.Focus() })

$form.Controls.Add($textBox)
$form.ShowDialog() | Out-Null

$result | ConvertTo-Json
'''

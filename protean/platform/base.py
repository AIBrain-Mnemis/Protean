"""Cross-platform abstraction protocol.

Each platform backend implements this protocol to provide OS-specific
capabilities: window info, screen capture, input simulation, process info.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class WindowInfo:
    """Information about the currently active window."""

    pid: int
    process_name: str
    window_title: str
    bundle_id: str = ""  # macOS bundle identifier
    # Window geometry (logical coordinates)
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


@dataclass
class ClipboardContent:
    """Structured clipboard content."""

    kind: str = "empty"  # "text" | "files" | "image" | "empty"
    text: str = ""
    files: list[str] = field(default_factory=list)
    image_width: int = 0
    image_height: int = 0


@dataclass(frozen=True)
class DisplayInfo:
    """Information about a display/monitor."""

    display_id: int
    display_index: int  # 1-based index matching macOS screencapture -D convention
    width: int
    height: int
    origin_x: int = 0  # display origin in global coordinates
    origin_y: int = 0
    scale_factor: float = 1.0  # HiDPI scale
    is_primary: bool = False


@dataclass
class ScreenshotResult:
    """Result of a screenshot capture."""

    image_bytes: bytes
    width: int
    height: int
    format: str = "png"
    display_id: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ElementInfo:
    """Accessibility element at a screen point."""

    role: str
    label: str
    center_x: int
    center_y: int
    width: int
    height: int


@runtime_checkable
class Platform(Protocol):
    """Protocol that each OS backend must implement."""

    @property
    def name(self) -> str:
        """Platform name: 'macos', 'windows', 'linux'."""
        ...

    # ── Window info ──────────────────────────────────────

    def get_active_window(self) -> WindowInfo | None:
        """Return info about the currently focused window."""
        ...

    def get_window_at_point(self, x: int, y: int) -> WindowInfo | None:
        """Return the topmost window containing the given screen coordinates.

        This method uses geometric hit-testing against all on-screen windows,
        including menu-bar / accessory-policy apps that are invisible to
        ``get_active_window()``.  Falls back to ``get_active_window()`` when
        hit-testing is not available.
        """
        ...

    def list_windows(self) -> list[WindowInfo]:
        """List visible application windows."""
        ...

    def list_notifications(self) -> list[WindowInfo]:
        """List active notification and overlay windows."""
        ...

    # ── Display info ─────────────────────────────────────

    def get_displays(self) -> list[DisplayInfo]:
        """List all displays."""
        ...

    def get_cursor_position(self) -> tuple[int, int]:
        """Return current cursor position in global coordinates (x, y)."""
        ...

    # ── Screen recording ─────────────────────────────────

    def start_screen_recording(
        self,
        output_path: Path,
        display_index: int = 1,
        *,
        show_clicks: bool = True,
        capture_audio: bool = False,
    ) -> None:
        """Start recording the screen to a video file.

        Args:
            output_path: Where to write the video file.
            display_index: 1-based display index (1 = primary).
            show_clicks: Show click markers in the recording.
            capture_audio: Capture system audio in the recording.
        """
        ...

    def stop_screen_recording(self) -> Path | None:
        """Stop recording and return the output file path."""
        ...

    def capture_display(self, display_index: int, output_path: Path) -> None:
        """Capture a single display to an image file.

        Args:
            display_index: 1-based display index.
            output_path: Where to write the image.
        """
        ...

    # ── Power / session management ───────────────────────

    def keep_awake(self) -> AbstractContextManager[None]:
        """Context manager that prevents the OS from sleeping or auto-locking
        the workstation while an agent run is in progress.

        Scope is the entire `with` block (process-level assertion); no
        per-step heartbeat is needed. Implementations should fall back to a
        no-op if the OS-level mechanism is unavailable. Default is no-op.
        """
        return contextlib.nullcontext()

    # ── Input simulation (for future skill replay) ──────

    def click(self, x: int, y: int, button: str = "left") -> None:
        """Simulate a mouse click at logical coordinates."""
        ...

    def double_click(self, x: int, y: int) -> None:
        """Simulate a double-click at logical coordinates."""
        ...

    def move_cursor(self, x: int, y: int) -> None:
        """Move the mouse cursor to logical coordinates without clicking."""
        ...

    def scroll(self, x: int, y: int, direction: str = "down", amount: int = 3) -> None:
        """Simulate a scroll event at the given coordinates."""
        ...

    def type_text(self, text: str) -> None:
        """Simulate typing text."""
        ...

    def get_clipboard(self) -> ClipboardContent:
        """Return the current clipboard content."""
        ...

    def key_press(self, *keys: str) -> None:
        """Simulate key press (e.g., 'cmd', 'c' for Cmd+C)."""
        ...

    # ── Accessibility (UI element discovery) ─────────────

    def find_element(
        self, app: str, label: str, *, role: str = ""
    ) -> tuple[int, int] | None:
        """Find a UI element by accessibility label and return its center (x, y).

        Args:
            app: Application name (e.g. "Microsoft Outlook").
            label: Accessibility label/title of the element.
            role: Optional AX role filter (e.g. "AXButton", "AXTextField").

        Returns:
            (x, y) center coordinates, or None if not found.
        """
        ...

    def ax_press(self, app: str, label: str, *, role: str = "") -> bool:
        """Find a UI element by label and perform AXPress action on it.

        Unlike click (which uses coordinate-based CGEvent), this uses the
        native accessibility action. More reliable for web-rendered controls
        (e.g. WebKit inside Outlook/Teams) that re-render after state change.

        Args:
            app: Application name.
            label: Accessibility label of the element.
            role: Optional AX role filter.

        Returns:
            True if element was found and AXPress succeeded.
        """
        ...

    def select_option(
        self, app: str, label: str, value: str
    ) -> bool:
        """Select an option from a dropdown/popup.

        Uses AXShowMenu to open the dropdown, then finds and presses
        the option within the opened menu.

        Args:
            app: Application name.
            label: Accessibility label of the dropdown.
            value: Option text to select.

        Returns:
            True if the option was found and selected.
        """
        ...

    def find_menu_item(self, app: str, menu_path: str) -> bool:
        """Click a menu item by path (e.g. "Edit > Remove Background").

        Returns True if the menu item was found and clicked.
        """
        ...

    def list_menu_items(self, app: str, menu_path: str = "") -> list[str]:
        """List menu items at a given path.

        Args:
            app: Application name.
            menu_path: Path like "文件" or "文件 > 新建". Empty = top-level.

        Returns:
            List of menu item names.
        """
        ...

    def list_elements(self, app: str, max_depth: int = 8) -> list[str]:
        """List visible UI elements in the frontmost window.

        Args:
            app: Application name.
            max_depth: How deep to traverse the UI tree.

        Returns:
            List of formatted strings like "  AXButton: 'OK'"
        """
        ...

    def find_elements(self, app: str, query: str) -> list[ElementInfo]:
        """Fuzzy-search visible UI elements by text.

        Matches query as a case-insensitive substring against all
        text attributes (title, description, value) of every visible element.

        Args:
            app: Application name.
            query: Text to search for.

        Returns:
            List of matching ElementInfo with coordinates and size.
        """
        ...

    def element_at(self, x: int, y: int) -> ElementInfo | None:
        """Get the accessibility element at screen coordinates.

        Walks up from the hit-test leaf to the nearest actionable ancestor
        when the leaf itself has no useful label.

        Returns:
            ElementInfo with role, label, and geometry, or None.
        """
        ...

    def element_focused(self) -> ElementInfo | None:
        """Get the accessibility element that currently has keyboard focus.

        Used to attribute keyboard events (typing, hotkeys) to the focused
        control rather than whatever happens to be under the cursor.

        Returns:
            ElementInfo with role, label, and geometry, or None.
        """
        ...

    def get_element_role(self, app: str, label: str) -> str | None:
        """Return the AX role of an element found by label, or None."""
        ...

    def activate_app(self, app: str) -> None:
        """Bring an application to the foreground."""
        ...

    # ── Notifications ────────────────────────────────────

    def notify(self, title: str, message: str, *, sound: bool = True) -> None:
        """Show a system notification."""
        ...

    # ── Text prompt (Spotlight-style floating input) ───

    def prompt_text(
        self, title: str, placeholder: str = "", message: str = "",
    ) -> str | None:
        """Show a floating input box and wait for user to type + press Enter.

        Args:
            title: Short title shown in the panel title bar.
            placeholder: Placeholder text in the input field.
            message: Optional body text shown above the input field.

        Returns the entered text, or None if cancelled.
        """
        ...

    # ── Global hotkey ───────────────────────────────────

    def register_hotkey(self, keys: list[str], callback: Callable[[], None]) -> Callable[[], None]:
        """Register a global hotkey. Returns an unregister function.

        Args:
            keys: Key combination, e.g. ['cmd', 'shift', 'r']
            callback: Called when the hotkey is pressed.

        Returns:
            A callable that unregisters the hotkey when called.
        """
        ...


def get_platform() -> Platform:
    """Return the platform backend for the current OS."""
    if sys.platform == "darwin":
        from protean.platform.macos import MacOSPlatform

        return MacOSPlatform()
    elif sys.platform == "win32":
        from protean.platform.windows import WindowsPlatform

        return WindowsPlatform()
    elif sys.platform.startswith("linux"):
        from protean.platform.linux import LinuxPlatform

        return LinuxPlatform()
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def _find_display_at(
    displays: list[DisplayInfo], x: int, y: int,
) -> DisplayInfo | None:
    """Return the display containing the given point, or None."""
    for d in displays:
        if (
            d.origin_x <= x < d.origin_x + d.width
            and d.origin_y <= y < d.origin_y + d.height
        ):
            return d
    return None


def active_display(platform: Platform) -> DisplayInfo | None:
    """Return the DisplayInfo for the display the user is working on.

    Detection order: active window center → cursor position → primary → first.
    The active window is preferred because the executor's most recent
    ``activate_app(...)`` (or the user focusing a window) signals intent —
    that app's display is the one we want to screenshot and click on.
    Cursor is a weaker signal: in agent-driven sessions the user's hand
    often stays on the chat display while the target app lives elsewhere.
    """
    displays = platform.get_displays()
    if not displays:
        return None

    # Strategy 1: display containing active window center
    window = platform.get_active_window()
    if window and window.width > 0 and window.height > 0:
        d = _find_display_at(
            displays,
            window.x + window.width // 2,
            window.y + window.height // 2,
        )
        if d is not None:
            return d

    # Strategy 2: display containing cursor
    try:
        cx, cy = platform.get_cursor_position()
        d = _find_display_at(displays, cx, cy)
        if d is not None:
            return d
    except Exception:
        pass

    # Strategy 3: primary display, then first
    for d in displays:
        if d.is_primary:
            return d
    return displays[0]


def active_display_index(platform: Platform) -> int:
    """Return the display_index of the display the user is working on."""
    d = active_display(platform)
    return d.display_index if d is not None else 1


# ── Coordinate mapping ─────────────────────────────────

@dataclass(frozen=True)
class DisplayScale:
    """Maps a fixed API coordinate space onto an actual display.

    Executors show the model a screenshot resized to ``(api_w, api_h)`` and
    receive click coords in that same space. We then need to translate those
    back to real screen pixels on the chosen display, including the display's
    origin offset on multi-monitor setups.
    """

    api_w: int
    api_h: int
    actual_w: int
    actual_h: int
    origin_x: int = 0
    origin_y: int = 0

    def to_actual(self, x: int, y: int) -> tuple[int, int]:
        ax = int(round(x * self.actual_w / self.api_w)) + self.origin_x
        ay = int(round(y * self.actual_h / self.api_h)) + self.origin_y
        return ax, ay

    def to_api(self, actual_x: int, actual_y: int) -> tuple[int, int]:
        """Inverse of to_actual — project a real screen pixel back into API space."""
        ax = int(round((actual_x - self.origin_x) * self.api_w / self.actual_w))
        ay = int(round((actual_y - self.origin_y) * self.api_h / self.actual_h))
        return ax, ay


class CoordinateMapper:
    """Tracks the active display and maps API coords to screen pixels.

    Call ``refresh()`` whenever the executor takes a screenshot so the scale
    follows the user moving windows between monitors. ``to_actual()`` then
    translates a coord from API space (the screenshot the model sees) to
    a real screen pixel on the same display.
    """

    def __init__(self, platform: Platform, api_w: int, api_h: int) -> None:
        self._platform = platform
        self._api_w = api_w
        self._api_h = api_h
        # Identity scale until refresh() locks onto a display.
        self._scale = DisplayScale(api_w, api_h, api_w, api_h)
        self._display_index = 1

    @property
    def display_index(self) -> int:
        return self._display_index

    @property
    def scale(self) -> DisplayScale:
        return self._scale

    def refresh(self) -> DisplayInfo | None:
        """Re-detect the active display and update the scale + origin."""
        d = active_display(self._platform)
        if d is None:
            self._scale = DisplayScale(self._api_w, self._api_h, self._api_w, self._api_h)
            self._display_index = 1
            return None
        self._scale = DisplayScale(
            api_w=self._api_w,
            api_h=self._api_h,
            actual_w=d.width,
            actual_h=d.height,
            origin_x=d.origin_x,
            origin_y=d.origin_y,
        )
        self._display_index = d.display_index
        return d

    def to_actual(self, x: int, y: int) -> tuple[int, int]:
        return self._scale.to_actual(x, y)

    def to_api(self, actual_x: int, actual_y: int) -> tuple[int, int]:
        return self._scale.to_api(actual_x, actual_y)


# ── Screenshot preparation ─────────────────────────────

# Default target resolution and quality for LLM consumption
LLM_SCREENSHOT_WIDTH = 1024
LLM_SCREENSHOT_HEIGHT = 768
LLM_JPEG_QUALITY = 70


def prepare_screenshot_for_llm(
    image_bytes: bytes,
    *,
    max_width: int = LLM_SCREENSHOT_WIDTH,
    max_height: int = LLM_SCREENSHOT_HEIGHT,
    quality: int = LLM_JPEG_QUALITY,
    exact_size: bool = False,
) -> tuple[bytes, str]:
    """Resize and compress a screenshot for LLM consumption.

    Accepts any image format (PNG, JPEG, etc.) and returns JPEG bytes.

    Args:
        max_width, max_height: Target dimensions.
        quality: JPEG quality (1-100).
        exact_size: If True, resize to exact dimensions (for coordinate-
            bound use like CUA). If False (default), thumbnail to fit
            within bounds while preserving aspect ratio.

    Returns (jpeg_bytes, "image/jpeg").
    """
    import io

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        img = img.convert("RGB")
        if exact_size:
            img = img.resize(
                (max_width, max_height),
                Image.LANCZOS,  # type: ignore[attr-defined]
            )
        else:
            img.thumbnail(
                (max_width, max_height),
                Image.LANCZOS,  # type: ignore[attr-defined]
            )
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "image/jpeg"


# ── Keyboard helpers ───────────────────────────────────

# Canonical alias table for parse_key_combo. Keys are what models may emit
# (cross-platform / verbose names); values are what Platform.key_press
# expects. Unmapped tokens (letters, digits, punctuation, function keys
# like "f1") pass through unchanged.
_KEY_ALIASES: dict[str, str] = {
    "return": "return",
    "enter": "return",
    "tab": "tab",
    "escape": "escape",
    "esc": "escape",
    "backspace": "delete",
    "delete": "forwarddelete",
    "space": "space",
    "super": "win",
    "command": "cmd",
    "cmd": "cmd",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "win": "win",
    "windows": "win",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
}


def parse_key_combo(text: str) -> list[str]:
    """Parse a '+'-joined key combo (e.g. 'cmd+c', 'ctrl+shift+s') into the
    list of canonical keys accepted by Platform.key_press. Cross-platform
    synonyms like 'command'/'cmd', 'option'/'alt', 'super'/'win' are
    normalized so models can speak in their preferred OS conventions.
    """
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    return [_KEY_ALIASES.get(part, part) for part in parts]

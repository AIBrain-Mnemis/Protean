"""Linux platform backend — stub for future implementation.

Will use:
- AT-SPI / python-xlib for window info
- xdotool for input simulation
- ffmpeg for screen capture/recording
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from protean.platform.base import (
    ClipboardContent,
    DisplayInfo,
    ElementInfo,
    MouseButton,
    Platform,
    ScrollDirection,
    WindowInfo,
)


class LinuxPlatform(Platform):
    @property
    def name(self) -> str:
        return "linux"

    def get_active_window(self) -> WindowInfo | None:
        raise NotImplementedError("Linux backend not yet implemented")

    def get_window_at_point(self, x: int, y: int) -> WindowInfo | None:
        raise NotImplementedError("Linux backend not yet implemented")

    def list_windows(self) -> list[WindowInfo]:
        raise NotImplementedError("Linux backend not yet implemented")

    def list_notifications(self) -> list[WindowInfo]:
        raise NotImplementedError("Linux backend not yet implemented")

    def get_displays(self) -> list[DisplayInfo]:
        raise NotImplementedError("Linux backend not yet implemented")

    def get_cursor_position(self) -> tuple[int, int]:
        raise NotImplementedError("Linux backend not yet implemented")

    def start_screen_recording(
        self,
        output_path: Path,
        display_index: int = 1,
        *,
        show_clicks: bool = True,
        capture_audio: bool = False,
    ) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def stop_screen_recording(self) -> Path | None:
        raise NotImplementedError("Linux backend not yet implemented")

    def capture_display(self, display_index: int, output_path: Path) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def click(
        self, x: int, y: int, button: MouseButton = "left", click_count: int = 1,
    ) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def move_cursor(self, x: int, y: int) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def scroll(
        self,
        x: int,
        y: int,
        direction: ScrollDirection = "down",
        amount: int = 3,
    ) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def type_text(self, text: str) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def get_clipboard(self) -> ClipboardContent:
        raise NotImplementedError("Linux backend not yet implemented")

    def key_press(self, *keys: str) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def find_element(
        self, app: str, label: str, *, role: str = ""
    ) -> tuple[int, int] | None:
        raise NotImplementedError("Linux backend not yet implemented")

    def ax_press(self, app: str, label: str, *, role: str = "") -> bool:
        raise NotImplementedError("Linux backend not yet implemented")

    def select_option(self, app: str, label: str, value: str) -> bool:
        raise NotImplementedError("Linux backend not yet implemented")

    def find_menu_item(self, app: str, menu_path: str) -> bool:
        raise NotImplementedError("Linux backend not yet implemented")

    def list_menu_items(self, app: str, menu_path: str = "") -> list[str]:
        raise NotImplementedError("Linux backend not yet implemented")

    def list_elements(self, app: str, max_depth: int = 8) -> list[str]:
        raise NotImplementedError("Linux backend not yet implemented")

    def find_elements(self, app: str, query: str) -> list[ElementInfo]:
        raise NotImplementedError("Linux backend not yet implemented")

    def element_at(self, x: int, y: int) -> ElementInfo | None:
        raise NotImplementedError("Linux backend not yet implemented")

    def element_focused(self) -> ElementInfo | None:
        raise NotImplementedError("Linux backend not yet implemented")

    def get_element_role(self, app: str, label: str) -> str | None:
        raise NotImplementedError("Linux backend not yet implemented")

    def activate_app(self, app: str) -> WindowInfo:
        raise NotImplementedError("Linux backend not yet implemented")

    def notify(self, title: str, message: str, *, sound: bool = True) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def prompt_text(
        self, title: str, placeholder: str = "", message: str = "",
    ) -> str | None:
        raise NotImplementedError("Linux backend not yet implemented")

    def register_hotkey(self, keys: list[str], callback: Callable[[], None]) -> Callable[[], None]:
        raise NotImplementedError("Linux backend not yet implemented")

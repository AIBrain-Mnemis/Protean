"""Linux platform backend — stub for future implementation.

Will use:
- AT-SPI / python-xlib for window info
- xdotool for input simulation
- ffmpeg for screen capture/recording
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from protean.platform.base import ClipboardContent, DisplayInfo, ElementInfo, Platform, WindowInfo


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

    def click(self, x: int, y: int, button: str = "left") -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def move_cursor(self, x: int, y: int) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def type_text(self, text: str) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def element_at(self, x: int, y: int) -> ElementInfo | None:
        return None

    def get_clipboard(self) -> ClipboardContent:
        return ClipboardContent()

    def key_press(self, *keys: str) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def notify(self, title: str, message: str, *, sound: bool = True) -> None:
        raise NotImplementedError("Linux backend not yet implemented")

    def register_hotkey(self, keys: list[str], callback: Callable[[], None]) -> Callable[[], None]:
        raise NotImplementedError("Linux backend not yet implemented")

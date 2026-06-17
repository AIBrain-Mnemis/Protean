from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from protean.executor.actions import ActionExecutor
from protean.executor.providers.computer_use import ComputerUseExecutor
from protean.platform.base import (
    LLM_SCREENSHOT_HEIGHT,
    LLM_SCREENSHOT_WIDTH,
    ClipboardContent,
    CoordinateMapper,
    DisplayInfo,
    ElementInfo,
    WindowInfo,
)


class FakePlatform:
    def __init__(self) -> None:
        self.activated_apps: list[str] = []
        self.clicked_points: list[tuple[int, int]] = []
        self.element_positions: dict[tuple[str, str], tuple[int, int]] = {
            ("Microsoft Teams", "Share"): (2200, 260),
        }

    @property
    def name(self) -> str:
        return "test"

    def get_active_window(self) -> WindowInfo | None:
        return WindowInfo(
            pid=123,
            process_name="Microsoft Teams",
            window_title="Meeting",
            x=2000,
            y=100,
            width=1200,
            height=800,
        )

    def get_window_at_point(self, x: int, y: int) -> WindowInfo | None:
        return self.get_active_window()

    def list_windows(self) -> list[WindowInfo]:
        return []

    def list_notifications(self) -> list[WindowInfo]:
        return []

    def get_displays(self) -> list[DisplayInfo]:
        return [
            DisplayInfo(
                display_id=1,
                display_index=1,
                width=1728,
                height=1117,
                origin_x=0,
                origin_y=0,
                is_primary=True,
            ),
            DisplayInfo(
                display_id=2,
                display_index=2,
                width=2560,
                height=1440,
                origin_x=1728,
                origin_y=0,
                is_primary=False,
            ),
        ]

    def get_cursor_position(self) -> tuple[int, int]:
        return 0, 0

    def start_screen_recording(
        self,
        output_path,
        display_index=1,
        *,
        show_clicks=True,
        capture_audio=False,
    ) -> None:
        raise NotImplementedError

    def stop_screen_recording(self):
        raise NotImplementedError

    def click(self, x: int, y: int, button: str = "left") -> None:
        self.clicked_points.append((x, y))

    def double_click(self, x: int, y: int) -> None:
        self.clicked_points.append((x, y))

    def move_cursor(self, x: int, y: int) -> None:
        return None

    def scroll(self, x: int, y: int, direction: str = "down", amount: int = 3) -> None:
        return None

    def type_text(self, text: str) -> None:
        return None

    def get_clipboard(self) -> ClipboardContent:
        return ClipboardContent()

    def key_press(self, *keys: str) -> None:
        return None

    def find_element(self, app: str, label: str, *, role: str = "") -> tuple[int, int] | None:
        return self.element_positions.get((app, label))

    def ax_press(self, app: str, label: str, *, role: str = "") -> bool:
        return False

    def select_option(self, app: str, label: str, value: str) -> bool:
        return False

    def find_menu_item(self, app: str, menu_path: str) -> bool:
        return False

    def list_menu_items(self, app: str, menu_path: str = "") -> list[str]:
        return []

    def list_elements(self, app: str, max_depth: int = 8) -> list[str]:
        return []

    def find_elements(self, app: str, query: str) -> list[ElementInfo]:
        return []

    def element_at(self, x: int, y: int) -> ElementInfo | None:
        return None

    def element_focused(self) -> ElementInfo | None:
        return None

    def get_element_role(self, app: str, label: str) -> str | None:
        return None

    def activate_app(self, app: str) -> None:
        self.activated_apps.append(app)

    def notify(self, title: str, message: str, *, sound: bool = True) -> None:
        return None

    def keep_awake(self):
        return contextlib.nullcontext()

    def prompt_text(
        self, title: str, placeholder: str = "", message: str = "",
    ):
        return None

    def register_hotkey(self, keys, callback):
        raise NotImplementedError

    def capture_display(self, display_index: int, output_path: Path) -> None:
        from PIL import Image
        Image.new("RGB", (100, 100), (0, 0, 0)).save(str(output_path))


def _make_actions(platform: FakePlatform) -> ActionExecutor:
    mapper = CoordinateMapper(platform, LLM_SCREENSHOT_WIDTH, LLM_SCREENSHOT_HEIGHT)
    mapper.refresh()
    return ActionExecutor(platform, mapper)


def test_activate_app_reports_active_window_in_api_coordinates():
    platform = FakePlatform()
    actions = _make_actions(platform)

    result = asyncio.run(
        actions.dispatch(
            "activate_app",
            {"app": "Microsoft Teams"},
            include_screenshot=False,
        )
    )

    assert platform.activated_apps == ["Microsoft Teams"]
    assert result.text == (
        "Activated Microsoft Teams. Active window: \"Meeting\" "
        "(Microsoft Teams) at (109, 40) size 480x320"
    )


def test_left_click_maps_api_coordinates_to_active_display():
    platform = FakePlatform()
    actions = _make_actions(platform)

    result = asyncio.run(
        actions.dispatch(
            "left_click",
            {"x": 149, "y": 192},
            include_screenshot=False,
        )
    )

    assert platform.clicked_points == [(2100, 480)]
    assert result.text == "Clicked at (149, 192)"


def test_computer_use_tool_error_includes_schema_hint():
    executor = ComputerUseExecutor(
        api_key="test",
        model="gpt-4.1",
        platform=FakePlatform(),
    )

    result = asyncio.run(executor._execute_tool("left_click", {"x": "269, 959"}))
    expected = (
        "Error executing left_click: missing required field 'y'. "
        "Received input: {\"x\": \"269, 959\"}. "
        "Expected schema: {\"properties\": {"
        "\"x\": {\"description\": \"X coordinate (0-1023)\", \"type\": \"integer\"}, "
        "\"y\": {\"description\": \"Y coordinate (0 to screenshot height - 1)\", "
        "\"type\": \"integer\"}}, "
        "\"required\": [\"x\", \"y\"], \"type\": \"object\"}. "
        "Hint: Pass x and y as separate integer fields, "
        "not as a single comma-separated string, "
        "for example {\"x\": 100, \"y\": 200}."
    )

    assert result.text == expected


def test_computer_use_reasoning_truncated_as_content_placeholder():
    executor = ComputerUseExecutor(
        api_key="test",
        model="gpt-4.1",
        platform=FakePlatform(),
    )

    # Short reasoning kept as-is
    assert executor._truncate_reasoning("click the button") == "click the button"

    # Empty reasoning
    assert executor._truncate_reasoning("") == ""

    # Long reasoning truncated with marker. Threshold is _REASONING_TRUNCATE
    # (500); pick a size comfortably above it.
    long = "x" * 600
    result = executor._truncate_reasoning(long)
    assert len(result) == 500 + len(" [truncated]")
    assert result.endswith(" [truncated]")



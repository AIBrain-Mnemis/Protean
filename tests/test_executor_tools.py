from __future__ import annotations

import asyncio

from protean.executor.providers.computer_use import ComputerUseExecutor
from protean.platform.base import DisplayInfo, WindowInfo
from protean.realtime.tool_handlers import execute_tool


class FakePlatform:
    def __init__(self) -> None:
        self.activated_apps: list[str] = []
        self.clicked_points: list[tuple[int, int]] = []
        self.element_positions: dict[tuple[str, str], tuple[int, int]] = {
            ("Microsoft Teams", "Share"): (2200, 260),
        }

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
        self, output_path, display_index=1, *, show_clicks=True, capture_audio=False) -> None:
        raise NotImplementedError

    def stop_screen_recording(self):
        raise NotImplementedError

    def click(self, x: int, y: int, button: str = "left") -> None:
        self.clicked_points.append((x, y))

    def move_cursor(self, x: int, y: int) -> None:
        return None

    def type_text(self, text: str) -> None:
        return None

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

    def activate_app(self, app: str) -> None:
        self.activated_apps.append(app)

    def notify(self, title: str, message: str, *, sound: bool = True) -> None:
        return None

    def prompt_text(
        self, title: str, placeholder: str = "", message: str = "",
    ):
        return None

    def register_hotkey(self, keys, callback):
        raise NotImplementedError

    def capture_display(self, display_index, path):
        from PIL import Image
        Image.new("RGB", (100, 100), (0, 0, 0)).save(str(path))


def test_activate_app_reports_display(monkeypatch):
    platform = FakePlatform()
    monkeypatch.setattr("protean.realtime.tool_handlers.time.sleep", lambda _: None)

    result = execute_tool(platform, "activate_app", {"app": "Microsoft Teams"})

    assert platform.activated_apps == ["Microsoft Teams"]
    assert result == (
        "Activated Microsoft Teams."
        " Active window: Microsoft Teams title='Meeting' frame=(x=2000, y=100, w=1200, h=800)"
        " display=2 bounds=(x=1728, y=0, w=2560, h=1440, primary=False)"
    )


def test_click_at_uses_global_coordinates():
    platform = FakePlatform()

    result = execute_tool(platform, "click_at", {"x": 2100, "y": 480})

    assert platform.clicked_points == [(2100, 480)]
    assert result == (
        "Clicked at (2100, 480) using global coordinates."
        " Active window: Microsoft Teams title='Meeting' frame=(x=2000, y=100, w=1200, h=800)"
        " display=2 bounds=(x=1728, y=0, w=2560, h=1440, primary=False)"
    )


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
        "\"y\": {\"description\": \"Y coordinate (0-767)\", \"type\": \"integer\"}}, "
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


def test_click_returns_post_action_window_summary():
    platform = FakePlatform()

    result = execute_tool(
        platform,
        "click",
        {"app": "Microsoft Teams", "label": "Share"},
    )

    assert platform.clicked_points == [(2200, 260)]
    assert result == (
        "Clicked 'Share' at (2200, 260) in Microsoft Teams."
        " Active window: Microsoft Teams title='Meeting' frame=(x=2000, y=100, w=1200, h=800)"
        " display=2 bounds=(x=1728, y=0, w=2560, h=1440, primary=False)"
    )


def test_click_at_can_return_action_view(monkeypatch):
    platform = FakePlatform()
    monkeypatch.setattr(
        "protean.realtime.tool_handlers._capture_action_view",
        lambda p, x, y, tool_name: {
            "path": "/tmp/protean_action_views/action_view_test.png",
            "origin_x": 1780,
            "origin_y": 240,
            "width": 640,
            "height": 480,
        },
    )

    result = execute_tool(
        platform,
        "click_at",
        {"x": 2100, "y": 480, "include_action_view": True},
    )

    assert "Action view: path=/tmp/protean_action_views/action_view_test.png" in result
    assert "action_view_rect(global)=(x=1780, y=240, w=640, h=480)" in result


def test_move_can_return_action_view(monkeypatch):
    platform = FakePlatform()
    moved_points: list[tuple[int, int]] = []
    platform.move_cursor = lambda x, y: moved_points.append((x, y))
    monkeypatch.setattr(
        "protean.realtime.tool_handlers._capture_action_view",
        lambda p, x, y, tool_name: {
            "path": "/tmp/protean_action_views/action_view_move.png",
            "origin_x": 1728,
            "origin_y": 0,
            "width": 640,
            "height": 480,
        },
    )

    result = execute_tool(
        platform,
        "move",
        {"x": 40, "y": 30, "coordinate_mode": "window", "include_action_view": True},
    )

    assert moved_points == [(2040, 130)]
    assert "Action view: path=/tmp/protean_action_views/action_view_move.png" in result
    assert "action_view_rect(window)=(x=-272, y=-100, w=640, h=480)" in result


def test_click_at_display_mode_returns_display_relative_action_point(monkeypatch):
    platform = FakePlatform()
    monkeypatch.setattr(
        "protean.realtime.tool_handlers._capture_action_view",
        lambda p, x, y, tool_name: {
            "path": "/tmp/protean_action_views/action_view_display.png",
            "origin_x": 1728,
            "origin_y": 0,
            "width": 640,
            "height": 480,
        },
    )

    result = execute_tool(
        platform,
        "click_at",
        {
            "x": 100,
            "y": 50,
            "coordinate_mode": "display",
            "display": 2,
            "include_action_view": True,
        },
    )

    assert "action_view_rect(display)=(x=0, y=0, w=640, h=480)" in result


def test_click_at_uses_display_relative_coordinates():
    platform = FakePlatform()

    result = execute_tool(
        platform,
        "click_at",
        {"x": 100, "y": 50, "coordinate_mode": "display", "display": 2},
    )

    assert platform.clicked_points == [(1828, 50)]
    assert result == (
        "Clicked at (1828, 50) using display 2 coordinates."
        " Active window: Microsoft Teams title='Meeting' frame=(x=2000, y=100, w=1200, h=800)"
        " display=2 bounds=(x=1728, y=0, w=2560, h=1440, primary=False)"
    )


def test_move_uses_window_relative_coordinates():
    platform = FakePlatform()
    moved_points: list[tuple[int, int]] = []
    platform.move_cursor = lambda x, y: moved_points.append((x, y))

    result = execute_tool(
        platform,
        "move",
        {"x": 40, "y": 30, "coordinate_mode": "window"},
    )

    assert moved_points == [(2040, 130)]
    assert result == "Moved cursor to (2040, 130) using active window coordinates"

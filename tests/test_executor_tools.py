from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from protean.cli import main
from protean.executor.actions import GUI_TOOL_NAMES, ActionExecutor, tool_error_hint
from protean.executor.providers.computer_use import ComputerUseExecutor
from protean.platform.base import (
    LLM_SCREENSHOT_HEIGHT,
    LLM_SCREENSHOT_WIDTH,
    AccessibilityNode,
    AccessibilitySnapshot,
    ClipboardContent,
    CoordinateMapper,
    DisplayInfo,
    ElementInfo,
    Rect,
    WindowInfo,
)


class FakePlatform:
    def __init__(self) -> None:
        self.activated_apps: list[str] = []
        self.activated_windows: list[str] = []
        self.clicked_points: list[tuple[int, int]] = []
        self.dragged_points: list[tuple[int, int, int, int]] = []
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
            window_id="window-123",
            x=2000,
            y=100,
            width=1200,
            height=800,
        )

    def get_window_at_point(self, x: int, y: int) -> WindowInfo | None:
        return self.get_active_window()

    def list_windows(self) -> list[WindowInfo]:
        window = self.get_active_window()
        return [window] if window is not None else []

    def activate_window(self, window_id: str) -> WindowInfo:
        self.activated_windows.append(window_id)
        window = self.get_active_window()
        if window is None or window.window_id != window_id:
            raise RuntimeError(f"Window not found: {window_id}")
        return window

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

    def click(self, x: int, y: int, button: str = "left", click_count: int = 1) -> None:
        self.clicked_points.append((x, y))

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        self.dragged_points.append((from_x, from_y, to_x, to_y))

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
        return AccessibilitySnapshot(app="Microsoft Teams", window_title="Meeting")

    def activate_app(self, app: str) -> WindowInfo:
        self.activated_apps.append(app)
        window = self.get_active_window()
        if window is None:
            raise RuntimeError("No active window")
        return window

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
    mapper = CoordinateMapper(
        platform.get_displays()[1],
        LLM_SCREENSHOT_WIDTH,
        LLM_SCREENSHOT_HEIGHT,
    )
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


def test_list_windows_and_activate_window_select_target_display():
    platform = FakePlatform()
    mapper = CoordinateMapper(
        platform.get_displays()[0],
        LLM_SCREENSHOT_WIDTH,
        LLM_SCREENSHOT_HEIGHT,
    )
    actions = ActionExecutor(platform, mapper)

    listed = asyncio.run(
        actions.dispatch("list_windows", {}, include_screenshot=False)
    )
    activated = asyncio.run(
        actions.dispatch(
            "activate_window",
            {"window_id": "window-123"},
            include_screenshot=False,
        )
    )

    assert listed.text == (
        '[{"window_id": "window-123", "process_name": "Microsoft Teams", '
        '"window_title": "Meeting", "pid": 123, "display": 2, '
        '"global_x": 2000, "global_y": 100, "width": 1200, "height": 800, '
        '"active": true}]'
    )
    assert platform.activated_windows == ["window-123"]
    assert mapper.display_index == 2
    assert activated.text == (
        'Activated window window-123. Active window: "Meeting" '
        '(Microsoft Teams) on display 2 at (109, 40) size 480x320'
    )


def test_click_maps_api_coordinates_to_active_display():
    platform = FakePlatform()
    actions = _make_actions(platform)

    result = asyncio.run(
        actions.dispatch(
            "click",
            {"x": 149, "y": 192},
            include_screenshot=False,
        )
    )

    assert platform.clicked_points == [(2100, 480)]
    assert result.text == "Clicked at (149, 192)"


def test_drag_maps_each_endpoint_to_its_display_and_selects_target():
    platform = FakePlatform()
    mapper = CoordinateMapper(
        platform.get_displays()[0],
        LLM_SCREENSHOT_WIDTH,
        LLM_SCREENSHOT_HEIGHT,
    )
    actions = ActionExecutor(platform, mapper)

    result = asyncio.run(
        actions.dispatch(
            "drag",
            {
                "from_x": 512,
                "from_y": 331,
                "to_x": 512,
                "to_y": 288,
                "from_display": 1,
                "to_display": 2,
            },
            include_screenshot=False,
        )
    )

    assert platform.dragged_points == [(864, 558, 3008, 720)]
    assert mapper.display_index == 2
    assert result.text == "Dragged from display 1 (512, 331) to display 2 (512, 288)"


def test_drag_defaults_to_selected_display_and_rejects_invalid_display():
    platform = FakePlatform()
    mapper = CoordinateMapper(
        platform.get_displays()[1],
        LLM_SCREENSHOT_WIDTH,
        LLM_SCREENSHOT_HEIGHT,
    )
    actions = ActionExecutor(platform, mapper)

    asyncio.run(
        actions.dispatch(
            "drag",
            {"from_x": 100, "from_y": 100, "to_x": 200, "to_y": 200},
            include_screenshot=False,
        )
    )

    assert platform.dragged_points == [(1978, 250, 2228, 500)]
    assert mapper.display_index == 2

    with pytest.raises(ValueError, match="Display 999 is not available"):
        asyncio.run(
            actions.dispatch(
                "drag",
                {
                    "from_x": 100,
                    "from_y": 100,
                    "to_x": 200,
                    "to_y": 200,
                    "to_display": 999,
                },
                include_screenshot=False,
            )
        )


def test_coordinate_action_screenshot_includes_accessibility_context():
    class A11yPlatform(FakePlatform):
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
            return AccessibilitySnapshot(
                app=app or "TestApp",
                window_title="TestWindow",
                nodes=[
                    AccessibilityNode(
                        id="1",
                        role="button",
                        raw_role="AXButton",
                        label="Run",
                        value="",
                        description="",
                        x=2000,
                        y=120,
                        width=100,
                        height=40,
                        depth=1,
                        actions=("press",),
                    ),
                ],
            )

    platform = A11yPlatform()
    actions = _make_actions(platform)

    result = asyncio.run(actions.dispatch("click", {"x": 10, "y": 10}))

    assert result.screenshot_b64 is not None
    assert result.text is not None
    assert "Accessibility context" in result.text
    assert "button 'Run'" in result.text


def test_screenshot_reports_display_count_and_omits_offscreen_accessibility():
    platform = FakePlatform()
    mapper = CoordinateMapper(
        platform.get_displays()[0],
        LLM_SCREENSHOT_WIDTH,
        LLM_SCREENSHOT_HEIGHT,
    )
    actions = ActionExecutor(platform, mapper)

    result = actions.screenshot(display=1)

    assert result.text is not None
    assert "Screenshot: display 1 of 2" in result.text
    assert "Focused window:" not in result.text
    assert (
        "Accessibility context unavailable: focused window is outside display 1."
        in result.text
    )


def test_screenshot_reports_focused_window_on_selected_display():
    platform = FakePlatform()
    actions = _make_actions(platform)

    result = actions.screenshot(display=2)

    assert result.text is not None
    assert "Screenshot: display 2 of 2" in result.text
    assert "Focused window: id='window-123'" in result.text


def test_window_tools_are_registered_for_all_gui_consumers():
    assert {"list_windows", "activate_window"} <= GUI_TOOL_NAMES


def test_mcp_help_lists_every_registered_gui_tool():
    result = CliRunner().invoke(main, ["mcp", "--help"])

    assert result.exit_code == 0
    for name in GUI_TOOL_NAMES:
        assert name in result.output


def test_computer_use_tool_error_includes_schema_hint():
    executor = ComputerUseExecutor(
        api_key="test",
        model="gpt-4.1",
        platform=FakePlatform(),
    )

    result = asyncio.run(executor._execute_tool("click", {"x": "269, 959"}))
    expected = (
        "Error executing click: missing required field 'y'. "
        "Received input: {\"x\": \"269, 959\"}. "
        "Expected schema: {\"properties\": {"
        "\"button\": {\"description\": \"Mouse button to click (default 'left')\", "
        "\"enum\": [\"left\", \"right\", \"middle\"], \"type\": \"string\"}, "
        "\"click_count\": {\"description\": \"Number of clicks at this point, "
        "e.g. 2 for double-click (default 1)\", \"type\": \"integer\"}, "
        "\"include_screenshot\": {\"description\": \"Set to false when this call is "
        "one of several independent actions "
        "you are issuing in the same turn and it is NOT the last one \\u2014 "
        "skips the screenshot in the result to save tokens. Default true.\", "
        "\"type\": \"boolean\"}, "
        "\"x\": {\"description\": \"X coordinate (0-1023)\", \"type\": \"integer\"}, "
        "\"y\": {\"description\": \"Y coordinate (0 to screenshot height - 1)\", "
        "\"type\": \"integer\"}}, "
        "\"required\": [\"x\", \"y\"], \"type\": \"object\"}. "
        "Hint: Pass x and y as separate integer fields, "
        "not as a single comma-separated string, "
        "for example {\"x\": 100, \"y\": 200}."
    )

    assert result.text == expected


def test_computer_use_drag_error_hint_uses_drag_fields():
    hint = tool_error_hint("drag", {"from_x": "100, 200"})
    assert "from_x" in hint and "from_y" in hint

    hint = tool_error_hint(
        "drag", {"from_x": 1, "from_y": 2, "to_x": "300, 400"},
    )
    assert "to_x" in hint and "to_y" in hint

    hint = tool_error_hint(
        "drag", {"from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4},
    )
    assert hint == "Pass integer from_x, from_y, to_x, to_y fields that match the tool schema"


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



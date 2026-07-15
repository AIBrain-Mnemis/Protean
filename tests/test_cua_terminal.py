"""Unit tests for the CUA terminal MCP integration.

Verifies that the optional `run_terminal_command` tool is wired into the
per-instance tool list when ``enable_terminal=True``, and absent when it's
disabled. Also tests truncation, PID extraction, notification drain,
and interactive prompt detection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from protean.executor.providers import computer_use as cu
from protean.platform.base import DisplayInfo


def _make_executor(enable_terminal: bool):
    """Construct a ComputerUseExecutor with create_sync_client mocked out."""
    fake_client = MagicMock()
    platform = MagicMock()
    platform.get_displays.return_value = [
        DisplayInfo(
            display_id=1,
            display_index=1,
            width=1920,
            height=1080,
            is_primary=True,
        )
    ]
    platform.get_active_window.return_value = None
    platform.get_cursor_position.return_value = (0, 0)
    with patch.object(
        cu, "create_sync_client",
        return_value=(fake_client, "claude-fake", False),
    ):
        return cu.ComputerUseExecutor(
            api_key="fake",
            model="claude-fake",
            platform=platform,
            enable_terminal=enable_terminal,
        )


def test_terminal_tool_present_when_enabled() -> None:
    ex = _make_executor(enable_terminal=True)
    assert "run_terminal_command" in ex._tools_by_name
    schema = ex._tools_by_name["run_terminal_command"]["input_schema"]
    assert "command" in schema["properties"]
    assert schema["required"] == ["command"]


def test_terminal_tool_absent_when_disabled() -> None:
    ex = _make_executor(enable_terminal=False)
    assert "run_terminal_command" not in ex._tools_by_name
    # Sanity: base tools still wired up.
    assert "screenshot" in ex._tools_by_name
    assert "click" in ex._tools_by_name


def test_default_terminal_command() -> None:
    ex = _make_executor(enable_terminal=True)
    assert ex._mcp_terminal_command[0] == "npx"
    assert "@wonderwhy-er/desktop-commander@latest" in ex._mcp_terminal_command


def test_truncate_terminal_output_short_unchanged() -> None:
    s = "hello world"
    assert cu._truncate_terminal_output(s, 1024) == s


def test_truncate_terminal_output_long_marked() -> None:
    s = "x" * 20000
    out = cu._truncate_terminal_output(s, 1024)
    assert "truncated" in out
    assert "pipe through tail/head" in out
    assert len(out.encode("utf-8")) < 2048


def test_extract_pid_from_text() -> None:
    fake = MagicMock()
    fake.structuredContent = None
    fake.content = [MagicMock(text="Started process. PID: 1234. Initial output...")]
    assert cu._extract_pid(fake) == 1234


def test_extract_pid_from_structured() -> None:
    fake = MagicMock()
    fake.structuredContent = {"pid": 42}
    fake.content = []
    assert cu._extract_pid(fake) == 42


def test_terminal_mcp_requires_command() -> None:
    with pytest.raises(ValueError):
        cu._TerminalMCP([])


def test_terminal_mcp_has_notification_queue() -> None:
    term = cu._TerminalMCP(["echo", "test"])
    assert isinstance(term.notifications, list)
    assert len(term.notifications) == 0


def test_interactive_prompt_detection() -> None:
    """Verify the regex catches common interactive prompts."""
    positives = [
        "Password:",
        "Enter passphrase:",
        "[Y/n]",
        "[y/N]",
        "(y/n)",
        "Are you sure you want to continue?",
        "Press any key to continue",
        "Press ENTER to proceed",
        "(yes/no):",
    ]
    negatives = [
        "Downloading packages...",
        "Build succeeded",
        "password reset email sent",
        "100% complete",
    ]
    for text in positives:
        assert cu._INTERACTIVE_PROMPT_RE.search(text), f"Should match: {text!r}"
    for text in negatives:
        assert not cu._INTERACTIVE_PROMPT_RE.search(text), f"Should NOT match: {text!r}"


def test_drain_terminal_notifications_no_terminal() -> None:
    """Drain is a no-op when terminal is not active."""
    ex = _make_executor(enable_terminal=True)
    messages: list[dict] = []
    ex._drain_terminal_notifications(messages)
    assert len(messages) == 0


def test_drain_terminal_notifications_with_pending() -> None:
    """Drain appends pending notifications as a user message."""
    ex = _make_executor(enable_terminal=True)
    # Simulate a terminal with pending notifications.
    ex._terminal = MagicMock()
    ex._terminal.notifications = [
        "[Terminal PID 123] Process finished.",
        "[Terminal PID 456] Process idle.",
    ]
    messages: list[dict] = []
    ex._drain_terminal_notifications(messages)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "PID 123" in messages[0]["content"][0]["text"]
    assert "PID 456" in messages[0]["content"][0]["text"]
    # Notifications should be cleared.
    assert len(ex._terminal.notifications) == 0


def test_send_terminal_input_tool_present() -> None:
    """send_terminal_input tool is registered when terminal is enabled."""
    ex = _make_executor(enable_terminal=True)
    assert "send_terminal_input" in ex._tools_by_name
    schema = ex._tools_by_name["send_terminal_input"]["input_schema"]
    assert "pid" in schema["properties"]
    assert "input" in schema["properties"]
    assert set(schema["required"]) == {"pid", "input"}


def test_send_terminal_input_tool_absent_when_disabled() -> None:
    ex = _make_executor(enable_terminal=False)
    assert "send_terminal_input" not in ex._tools_by_name

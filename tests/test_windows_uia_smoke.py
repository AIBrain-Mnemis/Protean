"""Smoke test: confirm UIA element prefetch is wired and working on Windows.

Skipped on non-Windows. Catches the "extras forgotten" regression where
`uiautomation` is missing from the venv and `WindowsPlatform.element_*`
silently returns None for every event.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only UIA smoke test"
)


def test_uiautomation_importable() -> None:
    """uiautomation must import without error."""
    import uiautomation  # noqa: F401


def test_windows_platform_uia_probe_clean() -> None:
    """WindowsPlatform.__init__ must not flag UIA as unavailable."""
    from protean.platform.windows import WindowsPlatform

    p = WindowsPlatform()
    assert p._uia_unavailable_reason is None, (
        f"UIA should be available but probe failed: {p._uia_unavailable_reason}"
    )


def test_element_focused_does_not_raise() -> None:
    """element_focused() returns None or an ElementInfo — never raises."""
    from protean.platform.base import ElementInfo
    from protean.platform.windows import WindowsPlatform

    p = WindowsPlatform()
    el = p.element_focused()
    assert el is None or isinstance(el, ElementInfo)


def test_element_at_does_not_raise() -> None:
    """element_at() at cursor position returns None or ElementInfo — never raises."""
    from protean.platform.base import ElementInfo
    from protean.platform.windows import WindowsPlatform

    p = WindowsPlatform()
    x, y = p.get_cursor_position()
    el = p.element_at(x, y)
    assert el is None or isinstance(el, ElementInfo)

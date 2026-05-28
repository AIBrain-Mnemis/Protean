"""Test Platform AX (Accessibility) capabilities on macOS.

These tests require macOS with Accessibility permission granted.
They interact with real running applications.
"""

import sys

import pytest

# Skip entire module on non-macOS
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


@pytest.fixture
def platform():
    from protean.platform.macos import MacOSPlatform

    return MacOSPlatform()


class TestFindAppPid:
    def test_finds_finder(self, platform):
        pid = platform._find_app_pid("Finder")
        assert pid is not None
        assert pid > 0

    def test_finds_by_partial_name(self, platform):
        # "Finder" should match even if localized name is "访达"
        pid = platform._find_app_pid("Finder")
        assert pid is not None

    def test_returns_none_for_nonexistent(self, platform):
        pid = platform._find_app_pid("ThisAppDoesNotExist12345")
        assert pid is None


class TestFindElement:
    def test_returns_tuple_or_none(self, platform):
        result = platform.find_element("Finder", "File")
        # May be None if Finder isn't frontmost, but should not crash
        if result is not None:
            assert isinstance(result, tuple)
            assert len(result) == 2

    def test_nonexistent_app(self, platform):
        result = platform.find_element("ThisAppDoesNotExist12345", "Button")
        assert result is None

    def test_nonexistent_label(self, platform):
        result = platform.find_element("Finder", "ThisLabelDoesNotExist12345")
        assert result is None


class TestFindMenuItem:
    def test_returns_bool(self, platform):
        # Don't actually click — just verify the method doesn't crash
        # We test with a nonexistent menu item so nothing happens
        result = platform.find_menu_item("Finder", "NonExistent > FakeItem")
        assert result is False

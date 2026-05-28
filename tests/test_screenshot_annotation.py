"""Verify ScreenshotCapturer._annotate draws red markers on the native image."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from protean.platform.base import DisplayInfo
from protean.recorder.events import EventType, InputEvent, MouseButton
from protean.recorder.screenshotter import ScreenshotCapturer


def _make_capturer(tmp_path: Path) -> ScreenshotCapturer:
    display = DisplayInfo(
        display_id=1,
        display_index=1,
        width=1024,
        height=768,
        origin_x=0,
        origin_y=0,
        scale_factor=1.0,
    )
    return ScreenshotCapturer(
        platform=None,  # _annotate doesn't touch platform
        display_info=display,
        out_dir=tmp_path,
        display_index=0,
    )


def _has_red_near(img: Image.Image, x: int, y: int, radius: int = 40) -> bool:
    px = img.load()
    w, h = img.size
    x0, y0 = max(0, x - radius), max(0, y - radius)
    x1, y1 = min(w, x + radius), min(h, y + radius)
    for ix in range(x0, x1, 2):
        for iy in range(y0, y1, 2):
            r, g, b = px[ix, iy][:3]
            if r > 200 and g < 90 and b < 90:
                return True
    return False


def test_annotate_click_draws_red_circle(tmp_path: Path) -> None:
    cap = _make_capturer(tmp_path)
    img = Image.new("RGB", (1024, 768), (255, 255, 255))
    event = InputEvent(
        timestamp=1.0,
        event_type=EventType.MOUSE_CLICK,
        x=200,
        y=300,
        button=MouseButton.LEFT,
    )
    cap._annotate(img, event, display_width=1024, display_origin=(0, 0))
    assert _has_red_near(img, 200, 300), "red circle should appear near click point"
    # Far from the click point should remain white.
    assert not _has_red_near(img, 800, 600, radius=20)


def test_annotate_text_input_uses_last_click_bounds(tmp_path: Path) -> None:
    cap = _make_capturer(tmp_path)
    img1 = Image.new("RGB", (1024, 768), (255, 255, 255))
    click = InputEvent(
        timestamp=1.0,
        event_type=EventType.MOUSE_CLICK,
        x=400,
        y=400,
        button=MouseButton.LEFT,
        metadata={"element_bounds": [350, 380, 200, 40]},
    )
    cap._annotate(img1, click, display_width=1024, display_origin=(0, 0))
    assert _has_red_near(img1, 350, 380), "click should draw bounds rectangle"

    img2 = Image.new("RGB", (1024, 768), (255, 255, 255))
    typed = InputEvent(
        timestamp=2.0,
        event_type=EventType.TEXT_INPUT,
        text="hello",
    )
    cap._annotate(img2, typed, display_width=1024, display_origin=(0, 0))
    # Bounds rectangle on (350,380)-(550,420)
    assert _has_red_near(img2, 350, 380), "text input should reuse last click bounds"


def test_annotate_app_switch_is_noop(tmp_path: Path) -> None:
    cap = _make_capturer(tmp_path)
    img = Image.new("RGB", (1024, 768), (255, 255, 255))
    event = InputEvent(timestamp=1.0, event_type=EventType.APP_SWITCH)
    cap._annotate(img, event, display_width=1024, display_origin=(0, 0))
    # Whole image should remain white.
    assert not _has_red_near(img, 512, 384, radius=300)

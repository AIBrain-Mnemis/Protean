"""Input event data models for recording user behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    MOUSE_CLICK = "mouse_click"
    MOUSE_DOUBLE_CLICK = "mouse_double_click"
    MOUSE_MOVE = "mouse_move"
    MOUSE_SCROLL = "mouse_scroll"
    MOUSE_DRAG_START = "mouse_drag_start"
    MOUSE_DRAG_END = "mouse_drag_end"
    KEY_PRESS = "key_press"
    KEY_RELEASE = "key_release"
    KEY_COMBO = "key_combo"  # e.g. Cmd+C
    TEXT_INPUT = "text_input"  # accumulated typed text
    APP_SWITCH = "app_switch"  # active app changed
    SPEECH = "speech"  # microphone utterance detected by VAD, with ASR transcript


class MouseButton(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


@dataclass
class WindowContext:
    """Which window/process the event occurred in."""

    pid: int = 0
    process_name: str = ""
    window_title: str = ""
    bundle_id: str = ""


@dataclass
class InputEvent:
    """A single recorded input event with full context."""

    timestamp: float  # time.monotonic() seconds since recording start
    event_type: EventType
    end_timestamp: float = 0  # for accumulated events (scroll, text_input)
    # Mouse fields
    x: int = 0
    y: int = 0
    button: MouseButton | None = None
    scroll_dx: int = 0
    scroll_dy: int = 0
    # Keyboard fields
    key: str = ""  # key name, e.g. "a", "enter", "cmd"
    key_char: str = ""  # typed character if printable
    modifiers: list[str] = field(default_factory=list)  # ["cmd", "shift", ...]
    # Text accumulation
    text: str = ""  # for TEXT_INPUT events
    # Speech (mic utterance) fields
    audio_path: str = ""  # relative path to WAV inside recording dir
    audio_duration: float = 0.0  # seconds
    transcript: str = ""  # ASR result
    # Context
    window: WindowContext = field(default_factory=WindowContext)
    # v2 screenshot fields (relative paths within recording dir)
    screenshot_overview: str = ""
    screenshot_detail: str = ""
    screenshot_crop_info: str = ""
    # Extra metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_timestamp: bool = True) -> dict[str, Any]:
        window_dict: dict[str, Any] = {
            "pid": self.window.pid,
            "process_name": self.window.process_name,
            "window_title": self.window.window_title,
        }
        if self.window.bundle_id:
            window_dict["bundle_id"] = self.window.bundle_id
        d: dict[str, Any] = {
            "event_type": self.event_type.value,
            "window": window_dict,
        }
        if include_timestamp:
            d["timestamp"] = round(self.timestamp, 3)
        if self.end_timestamp:
            d["end_timestamp"] = round(self.end_timestamp, 3)
        if self.event_type in (
            EventType.MOUSE_CLICK,
            EventType.MOUSE_DOUBLE_CLICK,
            EventType.MOUSE_MOVE,
            EventType.MOUSE_DRAG_START,
            EventType.MOUSE_DRAG_END,
        ):
            d["x"] = self.x
            d["y"] = self.y
            if self.button:
                d["button"] = self.button.value
        if self.event_type == EventType.MOUSE_SCROLL:
            d["x"] = self.x
            d["y"] = self.y
            d["scroll_dx"] = self.scroll_dx
            d["scroll_dy"] = self.scroll_dy
        if self.event_type in (EventType.KEY_PRESS, EventType.KEY_RELEASE, EventType.KEY_COMBO):
            d["key"] = self.key
            if self.key_char:
                d["key_char"] = self.key_char
            if self.modifiers:
                d["modifiers"] = self.modifiers
        if self.event_type == EventType.TEXT_INPUT:
            d["text"] = self.text
        if self.event_type == EventType.SPEECH:
            d["transcript"] = self.transcript
            d["audio_path"] = self.audio_path
            d["audio_duration"] = round(self.audio_duration, 3)
        if self.screenshot_overview:
            shot: dict[str, Any] = {"overview": self.screenshot_overview}
            if self.screenshot_detail:
                shot["detail"] = self.screenshot_detail
            if self.screenshot_crop_info:
                shot["crop_info"] = self.screenshot_crop_info
            d["screenshot"] = shot
        if self.metadata:
            d["metadata"] = self.metadata
        return d

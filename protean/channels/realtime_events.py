"""Realtime event types shared by the bridge adapter and TeachSession.

These are the wire-agnostic, in-process event types emitted by the
bridge-backed realtime client and consumed by ``protean.realtime.session``,
``protean.realtime.transcript``, and ``protean.channels.realtime_llm_bridge``.

The legacy in-process ``RealtimeLLM`` (with its Gemini/OpenAI provider
backends) is gone — only the dataclasses survive so the bridge adapter can
fan TRTC/Realtime events into the shape TeachSession already understands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RealtimeEventType(str, Enum):
    TEXT = "text"
    TOOL_CALL = "tool_call"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    SCREEN_STATE = "screen_state"


@dataclass
class ScreenStateUpdate:
    """The bridge changed its screen-capture target.

    Used by TeachSession to track which surface the user can see, so it can
    notify the executor (CUA) and keep its screenshots consistent with what
    the user is actually viewing.
    """

    mode: str  # "off" | "observe" | "share"
    source: str  # "remote_screen" | "local_screen" | "null"
    source_label: str = ""
    fps: float = 0.0
    resolution: tuple[int, int] = (0, 0)
    # 1-based physical display index the bridge is sharing (None when not
    # in share mode, or when the bridge didn't report one — e.g. observe
    # mode). The executor should pass this to its screenshot/click tools
    # so it targets the same display the user is watching.
    display_index: int | None = None


@dataclass
class ToolCallRequest:
    """A function call from the realtime LLM."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class RealtimeEvent:
    """An event received from the realtime LLM (via the bridge adapter)."""

    type: RealtimeEventType
    text: str = ""
    tool_call: ToolCallRequest | None = None
    error: str = ""
    screen_state: ScreenStateUpdate | None = None

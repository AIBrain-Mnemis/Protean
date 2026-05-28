"""IPC protocol types for the Python <-> Electron bridge.

This module is the Python-side mirror of ``electron-bridge/src/protocol.ts``.
The two MUST stay in lockstep — the message-type union below and the one in
the TS file are diffed in CI.

This file is type declarations only; no runtime logic, no I/O.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

# ── Protocol version ─────────────────────────────────────────────────────

PROTOCOL_VERSION = 1


# ── Message type union (mirror of MessageType in protocol.ts) ────────────────

MessageType = Literal[
    # Handshake / lifecycle
    "hello",
    "welcome",
    "session_start",
    "session_started",
    "session_end",
    "session_ended",
    # Realtime conversation events (S→C)
    "assistant_text",
    "user_speech",
    "realtime_error",
    # Tool calls
    "tool_call",
    "tool_result",
    # Out-of-band (C→S)
    "notify",
    "send_text",
    "send_image",
    # State pushes (S→C)
    "room_state",
    "screen_state",
    # Recorder coordination (C→S)
    "recorder_state",
    # Liveness
    "ping",
    "pong",
    # Error envelope
    "error",
]


# ── Envelope (every TEXT message) ───────────────────────────────────────────


class Envelope(TypedDict):
    """Wire envelope wrapping every TEXT message."""

    v: int                # protocol version (must equal PROTOCOL_VERSION)
    type: MessageType
    id: str               # sender-unique correlation id
    ts: float             # monotonic seconds since session_start; 0 = pre-session
    payload: dict[str, Any]


# ── Handshake payloads ────────────────────────────────────────────────────────


class HelloPayload(TypedDict):
    protocol_version: int
    daemon_version: str
    daemon_pid: int


class WelcomePayload(TypedDict):
    bridge_version: str
    capabilities: list[str]


# ── Session lifecycle payloads ─────────────────────────────────────────────────────


class ToolDeclaration(TypedDict):
    """Tool declaration sent in session_start.realtime.tools[].

    Bridge MUST strip ``handler`` before forwarding to the Realtime provider.
    """

    name: str
    handler: Literal["python", "bridge"]
    description: str
    parameters: dict[str, Any]


class RealtimeConfig(TypedDict):
    voice: str
    input_sample_rate: int
    output_sample_rate: int
    video_fps_limit: int
    system_instruction: str
    tools: list[ToolDeclaration]


class SessionStartPayload(TypedDict):
    realtime: RealtimeConfig
    accept_call_id: str


class SessionStartedPayload(TypedDict):
    session_id: str


class TranscriptSummary(TypedDict):
    duration: float
    user_words: int
    assistant_words: int


class SessionEndPayload(TypedDict):
    reason: str


class SessionEndedPayload(TypedDict):
    reason: str
    transcript_summary: NotRequired[TranscriptSummary]


# ── Realtime event payloads ─────────────────────────────────────────────────────


class AssistantTextPayload(TypedDict):
    text: str


class UserSpeechPayload(TypedDict):
    text: str
    audio_duration: NotRequired[float]
    final: bool


class RealtimeErrorPayload(TypedDict):
    code: str
    message: str
    recoverable: bool


# ── Tool call payloads ───────────────────────────────────────────────────────────


class ToolCallPayload(TypedDict):
    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolResultPayload(TypedDict):
    in_reply_to: str
    call_id: str
    name: str
    message: str
    is_async: bool
    error: NotRequired[str]


# ── Out-of-band payloads ─────────────────────────────────────────────────────────


class NotifyPayload(TypedDict):
    text: str


class SendTextPayload(TypedDict):
    text: str


class SendImagePayload(TypedDict):
    mime: str
    image_b64: str
    caption: NotRequired[str]


# ── State push payloads ──────────────────────────────────────────────────────────────


class RemoteUser(TypedDict):
    user_id: str
    has_audio: bool
    has_video: bool
    has_screen: bool


RoomState = Literal["idle", "ringing", "connected", "ended", "error"]


class RoomStatePayload(TypedDict):
    state: RoomState
    call_id: str
    remote_users: list[RemoteUser]
    network_quality: int        # TRTC's onNetworkQuality, 0-6
    detail: str


ScreenMode = Literal["off", "observe", "share"]
ScreenSource = Literal["remote_screen", "local_screen", "null"]


class ScreenStatePayload(TypedDict):
    mode: ScreenMode
    source: ScreenSource
    fps: float
    resolution: tuple[int, int]
    source_label: NotRequired[str]
    # 1-based index of the captured physical display in the bridge's
    # primary-first ordering (display 1 = primary). Only present for
    # `mode='share'`. The executor uses this to target the same display
    # the talker is sharing.
    display_index: NotRequired[int]


# ── Recorder payload ────────────────────────────────────────────────────────────


class RecorderStatePayload(TypedDict):
    recording: bool
    recording_id: NotRequired[str]
    output_dir: NotRequired[str]


# ── Liveness payloads ──────────────────────────────────────────────────────────


class PingPayload(TypedDict):
    pass


class PongPayload(TypedDict):
    in_reply_to: str


# ── Error payload ──────────────────────────────────────────────────────────────


ErrorCode = Literal[
    "bad_envelope",
    "unknown_type",
    "version_mismatch",
    "unauthorized",
    "incompatible",
    "unsupported_local_tool",
    "payload_too_large",
    "not_in_session",
    "duplicate_session",
]


class ErrorPayload(TypedDict):
    code: str            # ErrorCode for protocol-defined errors; free-form for domain
    message: str
    in_reply_to: NotRequired[str]
    fatal: bool


# ── Evidence frame header (BINARY) ─────────────────────────────────────────────
#
# Wire layout: 1-byte type tag + 2-byte BE header_len + header JSON + JPEG bytes.
# Only the JSON header is typed here; framing is done in transport code.

EVIDENCE_FRAME_TAG = 0x01


class EvidenceFrameHeader(TypedDict):
    ts: float
    source: Literal["remote_screen", "local_screen"]
    phash: int
    w: int
    h: int


# ── Convenience: pre-session whitelist ─────────────────────────────────────────

PRE_SESSION_TYPES: frozenset[MessageType] = frozenset(
    {"hello", "welcome", "ping", "pong", "error", "room_state", "recorder_state"}
)

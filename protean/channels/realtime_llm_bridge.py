"""RealtimeLLM-compat adapter — bridge-backed shim mimicking the now-retired
in-process ``RealtimeLLM`` API.

TeachSession was originally written against an in-process realtime client that
ran the Gemini/OpenAI WebSocket directly from Python (audio + tools + text).
That client is gone — the Electron bridge owns the transport — but
TeachSession's call sites (``send_audio`` / ``send_image`` / ``send_text`` /
``send_notification`` / ``send_tool_result`` / ``receive`` / ``disconnect``)
still expect the same surface. This adapter provides it on top of
``RealtimeBridgeClient`` so the talker loop didn't have to change shape.

Notes on the move:
  - Connect: ``connect(config)`` is replaced by ``session_start(payload)``
    that takes the ``SessionStartPayload`` (system_instruction,
    tools, accept_call_id). Provider/api_key/model live in the bridge env.
  - Audio playback: the bridge renderer plays Gemini's PCM directly. Python
    never sees raw audio, so there is no ``RealtimeEventType.AUDIO`` anymore.
    ``send_audio`` is kept as a no-op for any leftover call sites.
  - Tool calls: bridge ``tool_call`` envelopes are fanned into the same
    ``RealtimeEvent`` queue TeachSession iterates, so the dispatch loop is
    unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from protean.channels._protocol import SessionStartPayload
from protean.channels.realtime_bridge import (
    BridgeEvent,
    BridgeProtocolError,
    RealtimeBridgeClient,
    ToolCall,
)
from protean.channels.realtime_events import (
    RealtimeEvent,
    RealtimeEventType,
    ScreenStateUpdate,
    ToolCallRequest,
)

log = logging.getLogger(__name__)


class BridgeBackedRealtimeLLM:
    """Drop-in replacement for the retired in-process ``RealtimeLLM`` that
    routes through the bridge.

    Construct with an already-connected ``RealtimeBridgeClient``; call
    :meth:`session_start` to begin a session; call :meth:`receive` for
    the unified event stream that TeachSession expects.
    """

    def __init__(self, bridge: RealtimeBridgeClient) -> None:
        self._bridge = bridge
        self._event_queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue()
        self._fanout_task: asyncio.Task[None] | None = None
        self._tools_task: asyncio.Task[None] | None = None
        self._connected = False
        # Maps the bridge envelope id we expose as ToolCallRequest.id back to
        # the realtime provider's call_id, so send_tool_result can pass both
        # to the bridge (which needs both: in_reply_to + the realtime id).
        self._pending_realtime_call_id: dict[str, str] = {}

    @property
    def provider(self) -> str:
        # Bridge owns the provider; we expose a fixed label.
        return "bridge"

    @property
    def evidence_buffer(self) -> Any:
        """Forward the bridge's evidence frame buffer to TeachSession."""
        return self._bridge.evidence_buffer

    async def session_start(self, payload: SessionStartPayload) -> None:
        """Begin a bridge session. Replaces the retired ``connect(config)``."""
        await self._bridge.session_start(payload)
        self._connected = True
        # Emit the unified CONNECTED event so TeachSession's start path
        # observes the same lifecycle marker as before.
        self._event_queue.put_nowait(
            RealtimeEvent(type=RealtimeEventType.CONNECTED),
        )
        # Start fanouts: bridge events -> realtime queue, bridge tools -> realtime queue.
        self._fanout_task = asyncio.create_task(self._fanout_events())
        self._tools_task = asyncio.create_task(self._fanout_tool_calls())

    # ── Outbound (matches the retired RealtimeLLM signature) ────────────

    async def send_audio(self, pcm: bytes) -> None:  # noqa: ARG002 - kept for API compat
        # The bridge renderer captures the mic and forwards PCM to Gemini.
        # Python no longer pushes audio; this is a no-op for any leftover
        # call sites.
        return

    async def send_image(self, jpeg: bytes, mime: str = "image/jpeg") -> None:
        if not self._connected:
            return
        import base64
        await self._bridge.send_image(mime, base64.b64encode(jpeg).decode("ascii"))

    async def send_text(self, text: str) -> None:
        if not self._connected:
            return
        await self._bridge.send_text(text)

    async def send_notification(self, text: str) -> None:
        if not self._connected:
            return
        await self._bridge.notify(text)

    async def send_tool_result(
        self,
        call_id: str,
        name: str,
        result: str,
        *,
        is_async: bool = False,
        error: str | None = None,
    ) -> None:
        """Forward a tool result back to the realtime LLM via the bridge.

        `call_id` here is the bridge envelope id we exposed as
        ``ToolCallRequest.id`` (the bridge's pending map is keyed by it).
        We pair it with the realtime provider's own call_id that the
        bridge stashed via :meth:`_dispatch_tool_call`, so the bridge can
        emit a properly-shaped ``tool_result`` envelope and Gemini sees
        the function response. Without this round-trip Gemini stays
        stuck in "tool call pending" state and ignores subsequent user
        audio.
        """
        if not self._connected:
            return
        realtime_call_id = self._pending_realtime_call_id.pop(call_id, "")
        if not realtime_call_id:
            log.warning(
                "send_tool_result: no realtime call_id for envelope %s "
                "(double-result or unknown tool call?)",
                call_id,
            )
            return
        try:
            await self._bridge.tool_result(
                in_reply_to=call_id,
                call_id=realtime_call_id,
                name=name,
                message=result,
                is_async=is_async,
                error=error,
            )
        except Exception:
            log.exception("Failed to forward tool_result to bridge")

    async def receive(self) -> AsyncIterator[RealtimeEvent]:
        """Yield unified ``RealtimeEvent`` stream (TeachSession's main loop)."""
        while True:
            event = await self._event_queue.get()
            yield event
            if event.type == RealtimeEventType.DISCONNECTED:
                return

    async def disconnect(self) -> None:
        self._connected = False
        for task in (self._fanout_task, self._tools_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._fanout_task = None
        self._tools_task = None
        try:
            await self._bridge.session_end(reason="disconnect")
        except BridgeProtocolError:
            pass
        self._event_queue.put_nowait(
            RealtimeEvent(type=RealtimeEventType.DISCONNECTED),
        )

    # ── Internal fanouts ────────────────────────────────────────────────

    async def _fanout_events(self) -> None:
        try:
            async for evt in self._bridge.events():
                self._dispatch_bridge_event(evt)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bridge event fanout crashed")

    async def _fanout_tool_calls(self) -> None:
        try:
            async for tc in self._bridge.tool_calls():
                self._dispatch_tool_call(tc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bridge tool_call fanout crashed")

    def _dispatch_bridge_event(self, evt: BridgeEvent) -> None:
        if evt.type == "assistant_text":
            self._event_queue.put_nowait(
                RealtimeEvent(
                    type=RealtimeEventType.TEXT,
                    text=str(evt.payload.get("text", "")),
                ),
            )
            return
        if evt.type == "user_speech":
            # Only log finalized transcripts to avoid spam from partial chunks.
            if evt.payload.get("final"):
                text = str(evt.payload.get("text", "")).strip()
                if text:
                    log.info("User said: %s", text)
            return
        if evt.type == "realtime_error":
            self._event_queue.put_nowait(
                RealtimeEvent(
                    type=RealtimeEventType.ERROR,
                    error=str(evt.payload.get("message", "")),
                ),
            )
            return
        if evt.type == "room_state":
            # State "ended" means the call hung up (user left, server hangup,
            # or local exit). TeachSession's `async for ... receive()` may be
            # idle waiting on the realtime LLM, so push DISCONNECTED to break
            # the loop promptly.
            state = str(evt.payload.get("state", ""))
            if state == "ended":
                log.info("room_state ended → unblocking TeachSession")
                self._event_queue.put_nowait(
                    RealtimeEvent(type=RealtimeEventType.DISCONNECTED),
                )
            return
        if evt.type == "screen_state":
            payload = evt.payload
            resolution = payload.get("resolution") or (0, 0)
            try:
                w, h = int(resolution[0]), int(resolution[1])
            except (TypeError, ValueError, IndexError):
                w, h = 0, 0
            raw_display = payload.get("display_index")
            display_index: int | None
            try:
                display_index = int(raw_display) if raw_display is not None else None
            except (TypeError, ValueError):
                display_index = None
            update = ScreenStateUpdate(
                mode=str(payload.get("mode", "off")),
                source=str(payload.get("source", "null")),
                source_label=str(payload.get("source_label", "")),
                fps=float(payload.get("fps", 0) or 0),
                resolution=(w, h),
                display_index=display_index,
            )
            self._event_queue.put_nowait(
                RealtimeEvent(
                    type=RealtimeEventType.SCREEN_STATE,
                    screen_state=update,
                ),
            )
            return
        # recorder_state: not surfaced to TeachSession yet.

    def _dispatch_tool_call(self, tc: ToolCall) -> None:
        # Expose the bridge envelope id as the ToolCallRequest id so that
        # TeachSession's `send_tool_result(tc.id, ...)` round-trips correctly
        # (bridge's pending map is keyed by envelope id). We stash the
        # realtime provider's call_id in _pending_realtime_call_id so the
        # adapter can forward it back to the bridge.
        self._pending_realtime_call_id[tc.msg_id] = tc.call_id
        self._event_queue.put_nowait(
            RealtimeEvent(
                type=RealtimeEventType.TOOL_CALL,
                tool_call=ToolCallRequest(
                    id=tc.msg_id,
                    name=tc.name,
                    arguments=tc.arguments,
                ),
            ),
        )

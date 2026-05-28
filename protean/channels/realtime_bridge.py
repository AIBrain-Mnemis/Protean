"""RealtimeBridgeClient — Python WS client for the Electron bridge.

Speaks the protocol shared with ``electron-bridge/src/protocol.ts``. Owns:
  - WS connection lifecycle (handshake, ping/pong, close)
  - Envelope serialization with monotonic ``ts`` since session_started
  - Sender-unique ``id`` allocation
  - Inbound dispatch to async event queue + correlation map for tool_result
  - 30s tool_call timeout
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from protean.channels._protocol import (
    PRE_SESSION_TYPES,
    PROTOCOL_VERSION,
    Envelope,
    HelloPayload,
    MessageType,
    SessionStartPayload,
    WelcomePayload,
)
from protean.channels.evidence_buffer import (
    EvidenceFrameBuffer,
    EvidenceFrameError,
    parse_evidence_frame,
)

log = logging.getLogger(__name__)

_HANDSHAKE_TIMEOUT_SEC = 5.0
_TOOL_CALL_TIMEOUT_SEC = 30.0


class BridgeProtocolError(Exception):
    """Bridge violated the IPC protocol or returned a fatal error."""


# ── Inbound event types (fanned out via :meth:`RealtimeBridgeClient.events`) ─


@dataclass
class BridgeEvent:
    """A non-tool inbound event from the bridge."""

    type: MessageType
    payload: dict[str, Any]
    raw: Envelope


@dataclass
class ToolCall:
    """A tool the bridge wants Python to handle (handler:"python")."""

    call_id: str  # realtime provider's id (echo this back in result)
    msg_id: str   # envelope id (used as in_reply_to)
    name: str
    arguments: dict[str, Any]


@dataclass
class _Pending:
    waiter: asyncio.Future[Envelope]
    deadline: float = field(default=0.0)


# ── Client ──────────────────────────────────────────────────────────────────


class RealtimeBridgeClient:
    """One-shot WS client. Reusable across sessions but not across bridges.

    Typical use::

        client = RealtimeBridgeClient(port, token)
        await client.connect(daemon_version="0.1.0")
        await client.session_start(payload)
        async for event in client.events():
            ...
    """

    def __init__(self, port: int, token: str, *, host: str = "127.0.0.1") -> None:
        self._url = f"ws://{host}:{port}/?token={token}"
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task[None] | None = None

        # Inbound fan-out
        self._events: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
        self._tool_calls: asyncio.Queue[ToolCall | None] = asyncio.Queue()
        self._pending: dict[str, _Pending] = {}
        self._timeout_task: asyncio.Task[None] | None = None
        self._evidence_buffer = EvidenceFrameBuffer()

        # Session state
        self._welcome: WelcomePayload | None = None
        self._session_started_at: float | None = None
        self._closed = asyncio.Event()
        self._id_counter = itertools.count(1)

    @property
    def evidence_buffer(self) -> EvidenceFrameBuffer:
        """Buffer of recently-pushed screen frames; flush() to drain."""
        return self._evidence_buffer

    # ── Connection ───────────────────────────────────────────────────────

    async def connect(self, daemon_version: str) -> WelcomePayload:
        """Open WS, exchange hello/welcome. Returns the bridge's welcome payload."""
        if self._ws is not None:
            raise RuntimeError("RealtimeBridgeClient already connected")

        self._ws = await websockets.connect(
            self._url,
            ping_interval=5.0,
            ping_timeout=15.0,
            max_size=4 * 1024 * 1024,  # WS binary frame size limit
        )
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._timeout_task = asyncio.create_task(self._timeout_loop())

        hello: HelloPayload = {
            "protocol_version": PROTOCOL_VERSION,
            "daemon_version": daemon_version,
            "daemon_pid": os.getpid(),
        }
        welcome_env = await self._request(
            "hello", hello, timeout=_HANDSHAKE_TIMEOUT_SEC, expect="welcome",
        )
        welcome: WelcomePayload = welcome_env["payload"]  # type: ignore[assignment]
        self._welcome = welcome
        log.info(
            "Bridge welcome: version=%s capabilities=%s",
            welcome.get("bridge_version"), welcome.get("capabilities"),
        )
        return welcome

    @property
    def welcome(self) -> WelcomePayload | None:
        return self._welcome

    async def close(self) -> None:
        """Close the WS and cancel background tasks."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                log.debug("WS close error", exc_info=True)
        for task in (self._recv_task, self._timeout_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._recv_task = None
        self._timeout_task = None
        self._ws = None
        # Sentinel-shutdown the queues so iterators terminate.
        self._events.put_nowait(None)
        self._tool_calls.put_nowait(None)
        self._closed.set()

    # ── Session lifecycle ────────────────────────────────────────────────

    async def session_start(self, payload: SessionStartPayload) -> Envelope:
        """Start a session. Awaits ``session_started`` from the bridge."""
        if self._session_started_at is not None:
            raise RuntimeError("Session already active")
        env = await self._request(
            "session_start", payload,
            timeout=_HANDSHAKE_TIMEOUT_SEC,
            expect="session_started",
        )
        self._session_started_at = time.monotonic()
        return env

    async def session_end(self, reason: str = "client") -> Envelope | None:
        """Close the current session; awaits ``session_ended``."""
        if self._session_started_at is None:
            return None
        try:
            env = await self._request(
                "session_end", {"reason": reason},
                timeout=_HANDSHAKE_TIMEOUT_SEC,
                expect="session_ended",
            )
        finally:
            self._session_started_at = None
        return env

    # ── Outbound (non-request) messages ──────────────────────────────────

    async def notify(self, text: str) -> None:
        """Out-of-band notification; bridge forwards to realtime provider."""
        await self._send_envelope("notify", {"text": text})

    async def send_text(self, text: str) -> None:
        await self._send_envelope("send_text", {"text": text})

    async def send_image(self, mime: str, image_b64: str, caption: str | None = None) -> None:
        payload: dict[str, Any] = {"mime": mime, "image_b64": image_b64}
        if caption is not None:
            payload["caption"] = caption
        await self._send_envelope("send_image", payload)

    async def tool_result(
        self,
        *,
        in_reply_to: str,
        call_id: str,
        name: str,
        message: str,
        is_async: bool,
        error: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "in_reply_to": in_reply_to,
            "call_id": call_id,
            "name": name,
            "message": message,
            "is_async": is_async,
        }
        if error is not None:
            payload["error"] = error
        await self._send_envelope("tool_result", payload)

    async def recorder_state(self, recording: bool, *, recording_id: str | None = None,
                             output_dir: str | None = None) -> None:
        payload: dict[str, Any] = {"recording": recording}
        if recording_id is not None:
            payload["recording_id"] = recording_id
        if output_dir is not None:
            payload["output_dir"] = output_dir
        await self._send_envelope("recorder_state", payload)

    # ── Inbound iteration ────────────────────────────────────────────────

    async def events(self) -> AsyncIterator[BridgeEvent]:
        """Non-tool inbound events: assistant_text, user_speech, room_state, …"""
        while True:
            evt = await self._events.get()
            if evt is None:
                return
            yield evt

    async def tool_calls(self) -> AsyncIterator[ToolCall]:
        """Tool calls forwarded from bridge (handler:"python" tools)."""
        while True:
            tc = await self._tool_calls.get()
            if tc is None:
                return
            yield tc

    # ── Internals ────────────────────────────────────────────────────────

    def _next_id(self) -> str:
        return f"py_{next(self._id_counter)}"

    def _ts(self) -> float:
        if self._session_started_at is None:
            return 0.0
        return time.monotonic() - self._session_started_at

    def _build(self, type_: MessageType, payload: dict[str, Any]) -> Envelope:
        return {
            "v": PROTOCOL_VERSION,
            "type": type_,
            "id": self._next_id(),
            "ts": self._ts(),
            "payload": payload,
        }

    async def _send_envelope(self, type_: MessageType, payload: dict[str, Any]) -> Envelope:
        if self._ws is None:
            raise BridgeProtocolError("Not connected")
        env = self._build(type_, payload)
        await self._ws.send(json.dumps(env))
        return env

    async def _request(
        self,
        type_: MessageType,
        payload: dict[str, Any],
        *,
        timeout: float,
        expect: MessageType,
    ) -> Envelope:
        env = self._build(type_, payload)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Envelope] = loop.create_future()
        self._pending[env["id"]] = _Pending(
            waiter=fut, deadline=time.monotonic() + timeout,
        )
        if self._ws is None:
            raise BridgeProtocolError("Not connected")
        await self._ws.send(json.dumps(env))
        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(env["id"], None)
            raise BridgeProtocolError(
                f"Timed out waiting for {expect} (in_reply_to={env['id']})"
            ) from e
        if reply["type"] == "error":
            raise BridgeProtocolError(
                f"Bridge error in reply to {env['id']}: {reply['payload']}"
            )
        if reply["type"] != expect:
            raise BridgeProtocolError(
                f"Unexpected reply type {reply['type']!r}, wanted {expect!r}"
            )
        return reply

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    self._handle_binary(raw)
                    continue
                self._handle_text(raw)
        except websockets.ConnectionClosed:
            log.info("Bridge WS closed")
        except Exception:
            log.exception("recv loop crashed")
        finally:
            self._closed.set()
            # Wake up any awaiters.
            for pending in self._pending.values():
                if not pending.waiter.done():
                    pending.waiter.set_exception(
                        BridgeProtocolError("Connection closed")
                    )
            self._pending.clear()
            self._events.put_nowait(None)
            self._tool_calls.put_nowait(None)

    def _handle_binary(self, raw: bytes) -> None:
        try:
            frame = parse_evidence_frame(raw)
        except EvidenceFrameError as e:
            log.warning("Dropping malformed binary frame: %s", e)
            return
        self._evidence_buffer.push(frame)

    def _handle_text(self, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except Exception:
            log.warning("Bad JSON from bridge: %r", raw[:200])
            return
        if not isinstance(obj, dict):
            log.warning("Non-object envelope from bridge: %r", obj)
            return

        env: Envelope = obj  # trust bridge schema; supervisor would have died otherwise

        # If it's a reply to one of our requests, route to the waiter.
        payload = env.get("payload")
        in_reply_to = (
            payload.get("in_reply_to") if isinstance(payload, dict) else None
        )
        if isinstance(in_reply_to, str) and in_reply_to in self._pending:
            pending = self._pending.pop(in_reply_to)
            if not pending.waiter.done():
                pending.waiter.set_result(env)
            return

        # Special: handshake `welcome` has no in_reply_to; match by type & first hello.
        if env["type"] == "welcome":
            for msg_id, pending in list(self._pending.items()):
                if not pending.waiter.done():
                    self._pending.pop(msg_id, None)
                    pending.waiter.set_result(env)
                    return

        # session_started / session_ended also return without in_reply_to in some
        # bridge implementations; match by type.
        if env["type"] in ("session_started", "session_ended"):
            for msg_id, pending in list(self._pending.items()):
                if not pending.waiter.done():
                    self._pending.pop(msg_id, None)
                    pending.waiter.set_result(env)
                    return

        # Tool calls go to a dedicated queue.
        if env["type"] == "tool_call":
            payload = env["payload"]
            self._tool_calls.put_nowait(
                ToolCall(
                    call_id=str(payload.get("call_id", "")),
                    msg_id=env["id"],
                    name=str(payload.get("name", "")),
                    arguments=dict(payload.get("arguments", {})),
                )
            )
            return

        # Pre-session state pushes and conversation events go to the events queue.
        if env["type"] in PRE_SESSION_TYPES or env["type"] in (
            "assistant_text", "user_speech", "realtime_error", "screen_state",
        ):
            self._events.put_nowait(
                BridgeEvent(type=env["type"], payload=dict(env["payload"]), raw=env),
            )
            return

        # Errors not matched to a pending request: surface as event.
        if env["type"] == "error":
            self._events.put_nowait(
                BridgeEvent(type="error", payload=dict(env["payload"]), raw=env),
            )
            return

        log.debug("Unhandled envelope: %s", env["type"])

    async def _timeout_loop(self) -> None:
        """Expire pending waiters past their deadline."""
        try:
            while True:
                await asyncio.sleep(0.5)
                now = time.monotonic()
                for msg_id in list(self._pending.keys()):
                    pending = self._pending[msg_id]
                    if pending.deadline and pending.deadline < now:
                        self._pending.pop(msg_id, None)
                        if not pending.waiter.done():
                            pending.waiter.set_exception(
                                BridgeProtocolError(f"Request {msg_id} timed out")
                            )
        except asyncio.CancelledError:
            return

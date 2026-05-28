"""End-to-end test: spawn the Electron bridge from Python and run a mock session.

Validates bring-up + sync echo + clean session_end. Skips if the bridge
hasn't been built yet.

To build the bridge:

    cd electron-bridge && npm install && npm run build

Then run::

    uv run pytest tests/test_bridge_e2e.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from protean.channels._protocol import SessionStartPayload
from protean.channels.bridge_supervisor import BridgeSupervisor
from protean.channels.realtime_bridge import (
    BridgeProtocolError,
    RealtimeBridgeClient,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIST = REPO_ROOT / "electron-bridge" / "dist" / "main.js"


pytestmark = pytest.mark.skipif(
    not BRIDGE_DIST.exists(),
    reason=(
        "electron-bridge not built. Run `cd electron-bridge && npm install "
        "&& npm run build` first."
    ),
)


def _minimal_session_start(call_id: str) -> SessionStartPayload:
    return {
        "realtime": {
            "voice": "Puck",
            "input_sample_rate": 16000,
            "output_sample_rate": 24000,
            "video_fps_limit": 2,
            "system_instruction": "test session",
            "tools": [
                {
                    "name": "observe_step",
                    "handler": "python",
                    "description": "test tool",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
                {
                    "name": "start_screen",
                    "handler": "bridge",
                    "description": "test bridge tool",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            ],
        },
        "accept_call_id": call_id,
    }


async def _drain_until(
    client: RealtimeBridgeClient,
    type_filter: str,
    timeout: float = 3.0,
) -> dict | None:
    """Drain events until one matching ``type_filter`` arrives."""

    async def _go() -> dict | None:
        async for evt in client.events():
            if evt.type == type_filter:
                return evt.payload
        return None

    return await asyncio.wait_for(_go(), timeout=timeout)


async def test_bridge_handshake_and_session() -> None:
    sup = BridgeSupervisor()
    boot = await sup.start()
    assert boot.protocol == 1
    assert boot.port > 0
    assert len(boot.token) == 32  # crypto.randomBytes(16).toString('hex')

    client = RealtimeBridgeClient(boot.port, boot.token)
    try:
        welcome = await client.connect(daemon_version="0.1.0-test")
        assert welcome["bridge_version"]
        caps = welcome["capabilities"]
        assert "realtime.gemini" in caps
        assert "transport.local_mock" in caps
        # tools.local: must include the bridge-local tools.
        local_caps = [c for c in caps if c.startswith("tools.local:")]
        assert local_caps, "missing tools.local: capability"
        local_names = local_caps[0].removeprefix("tools.local:").split(",")
        for needed in ("start_screen", "stop_screen", "request_screenshot"):
            assert needed in local_names, f"{needed} missing from {local_names}"

        started = await client.session_start(_minimal_session_start("trtc-test-room-001"))
        assert started["type"] == "session_started"
        assert started["payload"]["session_id"]

        # Mock transport pushes connected ~50ms after enterRoom.
        room_state = await _drain_until(client, "room_state", timeout=2.0)
        assert room_state is not None, "no room_state received"
        assert room_state["state"] == "connected"
        assert room_state["call_id"] == "trtc-test-room-001"

        # Echo realtime: send_text -> assistant_text("echo: ...")
        await client.send_text("hello bridge")
        echoed = await _drain_until(client, "assistant_text", timeout=2.0)
        assert echoed is not None
        assert echoed["text"] == "echo: hello bridge"

        # notify -> assistant_text("notified: ...")
        await client.notify("status update")
        notified = await _drain_until(client, "assistant_text", timeout=2.0)
        assert notified is not None
        assert notified["text"] == "notified: status update"

        ended = await client.session_end(reason="test-done")
        assert ended is not None
        assert ended["type"] == "session_ended"
    finally:
        await client.close()
        await sup.stop()


async def test_bridge_rejects_unsupported_local_tool() -> None:
    """handler:"bridge" tool with name not in tools.local: -> session_start fails."""
    sup = BridgeSupervisor()
    boot = await sup.start()
    client = RealtimeBridgeClient(boot.port, boot.token)
    try:
        await client.connect(daemon_version="0.1.0-test")

        bad_payload: SessionStartPayload = {
            "realtime": {
                "voice": "Puck",
                "input_sample_rate": 16000,
                "output_sample_rate": 24000,
                "video_fps_limit": 2,
                "system_instruction": "x",
                "tools": [
                    {
                        "name": "no_such_tool",
                        "handler": "bridge",
                        "description": "x",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                ],
            },
            "accept_call_id": "trtc-test-room-002",
        }

        with pytest.raises(BridgeProtocolError) as excinfo:
            await client.session_start(bad_payload)
        assert "unsupported_local_tool" in str(excinfo.value)
    finally:
        await client.close()
        await sup.stop()


async def test_bridge_dies_on_stdin_close() -> None:
    """sup.stop() closes our stdin -> bridge exits within 2s."""
    sup = BridgeSupervisor()
    boot = await sup.start()
    pid = boot.pid
    await sup.stop()
    # If sup.stop() returned, the bridge process is reaped. Just verify the
    # supervisor cleared its handle.
    assert sup._proc is None  # noqa: SLF001 — internal check is the point of the test
    assert pid > 0


async def test_bridge_local_tool_routing() -> None:
    """handler:"bridge" tool: simulated by mock realtime, handled inside bridge.

    Mock realtime has a backdoor: sending text "__tool__:NAME:JSON_ARGS" makes
    the mock emit a realtime tool_call. Routing in session.ts then dispatches
    based on the tool's `handler` field.

    For start_screen (handler:"bridge"), the bridge calls into its
    ScreenCaptureController. That controller asks the renderer to start
    capture (which may or may not succeed depending on display availability),
    then immediately replies to the realtime model. Either way: NO tool_call
    envelope reaches Python.
    """
    sup = BridgeSupervisor()
    boot = await sup.start()
    client = RealtimeBridgeClient(boot.port, boot.token)
    try:
        await client.connect(daemon_version="0.1.0-test")
        await client.session_start(_minimal_session_start("trtc-test-room-003"))
        # Drain the room_state(connected) event first.
        await _drain_until(client, "room_state", timeout=2.0)

        # Trigger a bridge-local tool via the mock backdoor.
        await client.send_text('__tool__:start_screen:{"mode":"observe"}')

        # Bridge handles locally and replies to mock realtime via
        # sendToolResponse, which the mock surfaces as an assistant_text.
        ack = await _drain_until(client, "assistant_text", timeout=2.0)
        assert ack is not None
        assert "tool start_screen" in ack["text"]
        # Real implementation reports "started in observe mode"; mock
        # variant (no renderer) reports "(no renderer; stub)".
        assert (
            "started in observe mode" in ack["text"]
            or "no renderer; stub" in ack["text"]
        ), f"unexpected ack: {ack['text']}"

        # Critically: NO tool_call envelope should reach Python for
        # bridge-handled tools.
        try:
            tc = await asyncio.wait_for(
                client.tool_calls().__anext__(), timeout=0.5,
            )
        except (asyncio.TimeoutError, StopAsyncIteration):
            tc = None
        assert tc is None, f"bridge-local tool leaked to Python: {tc}"

        await client.session_end(reason="test-done")
    finally:
        await client.close()
        await sup.stop()


async def test_bridge_python_tool_routing() -> None:
    """handler:"python" tool: bridge forwards as tool_call IPC, awaits tool_result."""
    sup = BridgeSupervisor()
    boot = await sup.start()
    client = RealtimeBridgeClient(boot.port, boot.token)
    try:
        await client.connect(daemon_version="0.1.0-test")
        await client.session_start(_minimal_session_start("trtc-test-room-004"))
        await _drain_until(client, "room_state", timeout=2.0)

        # Trigger a python-routed tool via the mock backdoor.
        await client.send_text(
            '__tool__:observe_step:{"intent":"i","action":"a"}'
        )

        # Python receives tool_call.
        tc = await asyncio.wait_for(
            client.tool_calls().__anext__(), timeout=2.0,
        )
        assert tc.name == "observe_step"
        assert tc.arguments == {"intent": "i", "action": "a"}
        assert tc.call_id.startswith("mock-call-")

        # Python responds; bridge forwards to mock realtime which acks via
        # an assistant_text.
        await client.tool_result(
            in_reply_to=tc.msg_id,
            call_id=tc.call_id,
            name=tc.name,
            message="ok step recorded",
            is_async=False,
        )
        ack = await _drain_until(client, "assistant_text", timeout=2.0)
        assert ack is not None
        assert "tool observe_step" in ack["text"]
        assert "message=ok step recorded" in ack["text"]

        await client.session_end(reason="test-done")
    finally:
        await client.close()
        await sup.stop()

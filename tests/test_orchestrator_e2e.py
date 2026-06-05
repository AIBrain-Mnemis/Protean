"""End-to-end orchestrator test.

Spawns the bridge subprocess with PROTEAN_PRESENCE_URL pointing at a
fake matchmaking server (in-process aiohttp-style asyncio server using
the stdlib). Drives the full happy path:

  1. Bridge boots → presence client starts heartbeating
  2. Fake server returns assignment in 2nd heartbeat
  3. Bridge pushes room_state(ringing) via WS to Python
  4. Python sends session_start
  5. Bridge calls POST /confirm
  6. Bridge pushes room_state(connected)
  7. Fake server returns shouldHangup=true
  8. Bridge pushes room_state(ended)
  9. Verify NO DELETE was called (server-driven hangup, per API §3.1)

Skips if the bridge isn't built.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from protean.channels._protocol import SessionStartPayload
from protean.channels.bridge_supervisor import BridgeSupervisor
from protean.channels.realtime_bridge import RealtimeBridgeClient

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIST = REPO_ROOT / "electron-bridge" / "dist" / "main.js"


pytestmark = pytest.mark.skipif(
    not BRIDGE_DIST.exists(),
    reason=(
        "electron-bridge not built. Run `cd electron-bridge && npm install "
        "&& npm run build` first."
    ),
)


# ── Fake matchmaking server ─────────────────────────────────────────────────


class FakeServer:
    """Minimal stdlib HTTP server simulating the matchmaking API.

    Exposes /api/bots/<id>/heartbeat (POST), /api/calls/<roomId>/confirm
    (POST), /api/calls/<roomId> (DELETE).

    State machine driven by `set_assignment()` and `set_should_hangup()`
    test hooks.
    """

    ROOM_ID = "room_orcetest"
    USER_SIG = "usig_test_xxxxxxxx"
    SDK_APP_ID = 1400000000

    def __init__(self) -> None:
        self.heartbeats: list[dict] = []
        self.confirms: list[str] = []
        self.deletes: list[tuple[str, dict]] = []
        self._assignment_active = False
        self._should_hangup = False
        self._confirmed = False
        self._server_status = "IDLE"
        self.runner: asyncio.AbstractServer | None = None
        self.port: int = 0

    def set_assignment(self, active: bool) -> None:
        self._assignment_active = active
        self._server_status = "RESERVED" if active else "IDLE"
        if not active:
            self._confirmed = False

    def set_should_hangup(self, value: bool) -> None:
        self._should_hangup = value

    async def start(self) -> int:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await self._handle(reader, writer)
            except Exception:
                writer.close()

        self.runner = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.runner.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.runner is not None:
            self.runner.close()
            await self.runner.wait_closed()
            self.runner = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        # Parse one HTTP request (small, no keep-alive).
        request_line = await reader.readline()
        if not request_line:
            writer.close()
            return
        method, path, _ = request_line.decode("utf-8").strip().split(" ", 2)

        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            k, _, v = line.decode("utf-8").partition(":")
            headers[k.strip().lower()] = v.strip()

        body_len = int(headers.get("content-length", "0"))
        body_bytes = await reader.readexactly(body_len) if body_len else b""
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            body = {}

        response = self._route(method, path, body)
        body_out = json.dumps(response).encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body_out)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n"
            + body_out
        )
        await writer.drain()
        writer.close()

    def _route(self, method: str, path: str, body: dict) -> dict:
        if method == "POST" and path.endswith("/heartbeat"):
            self.heartbeats.append(body)
            assignment = None
            if self._assignment_active and not self._confirmed:
                assignment = {
                    "roomId": self.ROOM_ID,
                    "userId": "user_botside",
                    "userSig": self.USER_SIG,
                    "sdkAppId": self.SDK_APP_ID,
                    "displayName": "Tester",
                    "reservedAt": 0,
                }
            # The bridge's HeartbeatResponse type expects
            # {success, result: {status, assignment, serverTime}}.
            return {
                "success": True,
                "result": {
                    "status": self._server_status,
                    "assignment": assignment,
                    "serverTime": 0,
                },
            }

        if method == "POST" and path.endswith("/confirm"):
            self.confirms.append(self.ROOM_ID)
            self._confirmed = True
            self._server_status = "BUSY"
            return {"ok": True, "status": "BUSY"}

        if method == "DELETE" and path.startswith("/api/calls/"):
            self.deletes.append((path.rsplit("/", 1)[-1], body))
            self._assignment_active = False
            self._confirmed = False
            self._server_status = "IDLE"
            return {"ok": True}

        return {}


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _wait_for(
    pred: Callable[[], bool], timeout: float, *, msg: str,
) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for: {msg}")


def _minimal_session_start(call_id: str) -> SessionStartPayload:
    return {
        "realtime": {
            "voice": "Puck",
            "input_sample_rate": 16000,
            "output_sample_rate": 24000,
            "video_fps_limit": 2,
            "system_instruction": "test",
            "tools": [
                {
                    "name": "observe_step",
                    "handler": "python",
                    "description": "x",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            ],
        },
        "accept_call_id": call_id,
    }


async def _drain_room_state(
    client: RealtimeBridgeClient, expected_state: str, *, timeout: float,
) -> dict:
    """Drain events until a room_state with the expected state arrives."""

    async def _go() -> dict | None:
        async for evt in client.events():
            if evt.type == "room_state" and evt.payload.get("state") == expected_state:
                return evt.payload
        return None

    payload = await asyncio.wait_for(_go(), timeout=timeout)
    assert payload is not None, f"no room_state(state={expected_state}) received"
    return payload


# ── The test ────────────────────────────────────────────────────────────────


@pytest.fixture
async def fake_server() -> AsyncIterator[FakeServer]:
    server = FakeServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_orchestrator_full_call_lifecycle(
    fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy-path end-to-end via fake matchmaking server."""
    presence_url = f"http://127.0.0.1:{fake_server.port}"
    monkeypatch.setenv("PROTEAN_PRESENCE_URL", presence_url)

    sup = BridgeSupervisor()
    boot = await sup.start()
    client = RealtimeBridgeClient(boot.port, boot.token)
    try:
        await client.connect(daemon_version="0.1.0-orchtest")

        # 1. With assignment NOT active, presence should heartbeat without
        #    pushing anything. Confirm by waiting briefly and asserting
        #    no envelopes.
        await _wait_for(
            lambda: len(fake_server.heartbeats) >= 1,
            timeout=4.0,
            msg="first heartbeat",
        )
        # The bridge sends empty heartbeat bodies; presence/status flows
        # the other way (server → bot via the response). Just verify the
        # bridge is heartbeating at all.

        # 2. Activate assignment server-side.
        fake_server.set_assignment(True)

        # 3. Bridge should push room_state(ringing) on the next heartbeat
        #    (≤ 2s later).
        ringing = await _drain_room_state(client, "ringing", timeout=5.0)
        assert ringing["call_id"] == fake_server.ROOM_ID
        assert "Tester" in ringing["detail"]

        # 4. Python side initiates session_start.
        started = await client.session_start(_minimal_session_start(fake_server.ROOM_ID))
        assert started["type"] == "session_started"

        # 5. Orchestrator should call POST /confirm and push connected.
        await _wait_for(
            lambda: len(fake_server.confirms) >= 1,
            timeout=4.0,
            msg="confirm called",
        )
        connected = await _drain_room_state(client, "connected", timeout=5.0)
        assert connected["call_id"] == fake_server.ROOM_ID
        assert connected["detail"] == "confirmed"

        # 6. Server-driven hangup.
        fake_server.set_should_hangup(True)
        ended = await _drain_room_state(client, "ended", timeout=5.0)
        assert ended["call_id"] == fake_server.ROOM_ID
        assert ended["detail"] == "server-hangup"

        # 7. Wind down session from Python side.
        ended_env = await client.session_end(reason="call-ended")
        assert ended_env is not None

        # 8. Bridge MUST NOT have called DELETE in this path (server-driven).
        await asyncio.sleep(0.5)
        assert fake_server.deletes == [], f"unexpected DELETEs: {fake_server.deletes}"

    finally:
        await client.close()
        await sup.stop()


async def test_orchestrator_python_driven_hangup_calls_delete(
    fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Python ends the session without server hangup, bridge calls DELETE."""
    presence_url = f"http://127.0.0.1:{fake_server.port}"
    monkeypatch.setenv("PROTEAN_PRESENCE_URL", presence_url)

    sup = BridgeSupervisor()
    boot = await sup.start()
    client = RealtimeBridgeClient(boot.port, boot.token)
    try:
        await client.connect(daemon_version="0.1.0-orchtest")
        fake_server.set_assignment(True)

        await _drain_room_state(client, "ringing", timeout=5.0)
        await client.session_start(_minimal_session_start(fake_server.ROOM_ID))
        await _drain_room_state(client, "connected", timeout=5.0)

        # Python-side hangup (no shouldHangup from server).
        await client.session_end(reason="client")

        # Bridge should call DELETE.
        await _wait_for(
            lambda: len(fake_server.deletes) >= 1,
            timeout=5.0,
            msg="DELETE called",
        )
        room_id, body = fake_server.deletes[0]
        assert room_id == fake_server.ROOM_ID
        assert body.get("by") == "bot"
        assert body.get("reason") == "user_hangup"

    finally:
        await client.close()
        await sup.stop()

"""EvidenceFrameBuffer — Python ring buffer for screen frames pushed by the bridge.

The bridge fans out every frame both to the realtime provider AND to Python
via BINARY WS frames. This module owns the 60-frame ring on the Python side
and exposes ``flush()`` that returns ``[(jpeg_bytes, ts_seconds)]`` for
``SkillBuilder.add_observed_step()``.

Wire format:

    [1 byte tag=0x01][2 bytes BE header_len][JSON header][JPEG bytes]

JSON header schema:

    {"ts": float, "source": "remote_screen"|"local_screen", "phash": int,
     "w": int, "h": int}
"""

from __future__ import annotations

import collections
import json
import logging
from dataclasses import dataclass
from typing import Literal

from protean.channels._protocol import EVIDENCE_FRAME_TAG, EvidenceFrameHeader

log = logging.getLogger(__name__)

DEFAULT_CAPACITY = 60


class EvidenceFrameError(Exception):
    """Bridge sent a malformed BINARY frame."""


@dataclass(frozen=True)
class EvidenceFrame:
    """One screen frame with its metadata."""

    jpeg: bytes
    ts: float
    source: Literal["remote_screen", "local_screen"]
    phash: int
    w: int
    h: int


class EvidenceFrameBuffer:
    """Bounded ring of recent evidence frames.

    Capacity matches the bridge's local screen.py buffer (60 frames @ 1fps =
    one minute of context). Older frames are evicted when the buffer fills.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._frames: collections.deque[EvidenceFrame] = collections.deque(
            maxlen=capacity,
        )

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def capacity(self) -> int:
        return self._frames.maxlen or 0

    def push(self, frame: EvidenceFrame) -> None:
        """Append a frame; oldest is evicted when full."""
        self._frames.append(frame)

    def flush(self) -> list[tuple[bytes, float]]:
        """Drain all frames; returns ``[(jpeg, ts)]`` oldest-first.

        Matches the shape consumed by ``SkillBuilder.add_observed_step``.
        """
        out = [(f.jpeg, f.ts) for f in self._frames]
        self._frames.clear()
        return out

    def peek(self) -> list[EvidenceFrame]:
        """Snapshot of buffered frames without draining (for diagnostics)."""
        return list(self._frames)


# ── Wire parsing ─────────────────────────────────────────────────────────────


def parse_evidence_frame(buf: bytes) -> EvidenceFrame:
    """Parse one BINARY WS frame.

    Raises ``EvidenceFrameError`` on malformed input.
    """
    if len(buf) < 3:
        raise EvidenceFrameError(f"binary frame too short: {len(buf)} bytes")
    tag = buf[0]
    if tag != EVIDENCE_FRAME_TAG:
        raise EvidenceFrameError(
            f"unexpected binary tag 0x{tag:02x} (want 0x{EVIDENCE_FRAME_TAG:02x})"
        )
    header_len = int.from_bytes(buf[1:3], byteorder="big", signed=False)
    if header_len == 0:
        raise EvidenceFrameError("zero header length")
    if 3 + header_len > len(buf):
        raise EvidenceFrameError(
            f"header_len={header_len} exceeds frame size {len(buf)}"
        )
    header_json = buf[3 : 3 + header_len]
    jpeg = bytes(buf[3 + header_len :])
    if not jpeg:
        raise EvidenceFrameError("empty JPEG payload")
    try:
        header_obj = json.loads(header_json.decode("utf-8"))
    except Exception as e:
        raise EvidenceFrameError(f"bad header JSON: {e}") from e

    for k in ("ts", "source", "phash", "w", "h"):
        if k not in header_obj:
            raise EvidenceFrameError(f"header missing field: {k!r}")
    source = header_obj["source"]
    if source not in ("remote_screen", "local_screen"):
        raise EvidenceFrameError(f"unknown source: {source!r}")
    typed: EvidenceFrameHeader = header_obj  # trust schema

    return EvidenceFrame(
        jpeg=jpeg,
        ts=float(typed["ts"]),
        source=typed["source"],
        phash=int(typed["phash"]),
        w=int(typed["w"]),
        h=int(typed["h"]),
    )

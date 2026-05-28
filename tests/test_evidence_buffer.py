"""Unit tests for EvidenceFrameBuffer + parse_evidence_frame.

Pure in-process; no bridge subprocess required.
"""

from __future__ import annotations

import json

import pytest

from protean.channels._protocol import EVIDENCE_FRAME_TAG
from protean.channels.evidence_buffer import (
    EvidenceFrame,
    EvidenceFrameBuffer,
    EvidenceFrameError,
    parse_evidence_frame,
)

# ── Buffer behavior ─────────────────────────────────────────────────────────


def _frame(ts: float, source: str = "local_screen") -> EvidenceFrame:
    return EvidenceFrame(
        jpeg=b"\xff\xd8\xff\xe0",  # synthetic JPEG magic + body stub
        ts=ts,
        source=source,  # type: ignore[arg-type]
        phash=int(ts * 1000),
        w=1280,
        h=720,
    )


def test_buffer_push_and_flush_oldest_first() -> None:
    buf = EvidenceFrameBuffer(capacity=5)
    for i in range(3):
        buf.push(_frame(float(i)))
    assert len(buf) == 3
    out = buf.flush()
    assert [ts for _, ts in out] == [0.0, 1.0, 2.0]
    # After flush the buffer is empty.
    assert len(buf) == 0
    assert buf.flush() == []


def test_buffer_evicts_oldest_when_full() -> None:
    buf = EvidenceFrameBuffer(capacity=3)
    for i in range(5):
        buf.push(_frame(float(i)))
    assert len(buf) == 3
    out = buf.flush()
    assert [ts for _, ts in out] == [2.0, 3.0, 4.0]


def test_buffer_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        EvidenceFrameBuffer(capacity=0)


# ── Wire parsing ────────────────────────────────────────────────────────────


def _build_frame(
    *,
    ts: float = 12.483,
    source: str = "local_screen",
    phash: int = 42,
    w: int = 1280,
    h: int = 720,
    jpeg: bytes = b"\xff\xd8\xff\xe0synthetic-jpeg-body",
    tag: int = EVIDENCE_FRAME_TAG,
) -> bytes:
    header = json.dumps({"ts": ts, "source": source, "phash": phash, "w": w, "h": h})
    header_bytes = header.encode("utf-8")
    return (
        bytes([tag])
        + len(header_bytes).to_bytes(2, byteorder="big", signed=False)
        + header_bytes
        + jpeg
    )


def test_parse_evidence_frame_happy_path() -> None:
    raw = _build_frame()
    frame = parse_evidence_frame(raw)
    assert frame.ts == 12.483
    assert frame.source == "local_screen"
    assert frame.phash == 42
    assert frame.w == 1280
    assert frame.h == 720
    assert frame.jpeg.startswith(b"\xff\xd8\xff\xe0")


def test_parse_evidence_frame_rejects_unknown_tag() -> None:
    raw = _build_frame(tag=0x99)
    with pytest.raises(EvidenceFrameError, match="unexpected binary tag"):
        parse_evidence_frame(raw)


def test_parse_evidence_frame_rejects_short_buffer() -> None:
    with pytest.raises(EvidenceFrameError, match="too short"):
        parse_evidence_frame(b"\x01\x00")


def test_parse_evidence_frame_rejects_zero_header_len() -> None:
    raw = bytes([EVIDENCE_FRAME_TAG, 0, 0]) + b"jpeg"
    with pytest.raises(EvidenceFrameError, match="zero header length"):
        parse_evidence_frame(raw)


def test_parse_evidence_frame_rejects_oversized_header() -> None:
    # Claim a 1000-byte header but provide only 10 bytes after the length prefix.
    raw = bytes([EVIDENCE_FRAME_TAG]) + (1000).to_bytes(2, "big") + b"only10byte"
    with pytest.raises(EvidenceFrameError, match="header_len"):
        parse_evidence_frame(raw)


def test_parse_evidence_frame_rejects_empty_jpeg() -> None:
    raw = _build_frame(jpeg=b"")
    with pytest.raises(EvidenceFrameError, match="empty JPEG"):
        parse_evidence_frame(raw)


def test_parse_evidence_frame_rejects_unknown_source() -> None:
    raw = _build_frame(source="something_else")
    with pytest.raises(EvidenceFrameError, match="unknown source"):
        parse_evidence_frame(raw)


def test_parse_evidence_frame_rejects_missing_field() -> None:
    # Manually build a header missing the `phash` field.
    header = json.dumps({"ts": 1.0, "source": "local_screen", "w": 10, "h": 10})
    header_bytes = header.encode("utf-8")
    raw = (
        bytes([EVIDENCE_FRAME_TAG])
        + len(header_bytes).to_bytes(2, "big")
        + header_bytes
        + b"jpeg"
    )
    with pytest.raises(EvidenceFrameError, match="missing field"):
        parse_evidence_frame(raw)


def test_parse_evidence_frame_rejects_bad_json_header() -> None:
    bogus = b"{not json"
    raw = bytes([EVIDENCE_FRAME_TAG]) + len(bogus).to_bytes(2, "big") + bogus + b"jpeg"
    with pytest.raises(EvidenceFrameError, match="bad header JSON"):
        parse_evidence_frame(raw)

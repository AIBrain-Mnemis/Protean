from __future__ import annotations

import json
from pathlib import Path

from protean.trajectories.markers import TrajectoryMarker, TrajectoryMarkerStore
from protean.trajectories.resolver import resolve_trajectory_slice


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_resolver_rejects_marker_from_different_explicit_session(tmp_path: Path):
    session = tmp_path / "codex.jsonl"
    _write_jsonl(session, [
        {
            "timestamp": "2026-05-28T01:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Start task"},
        },
        {
            "timestamp": "2026-05-28T01:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Done"},
        },
    ])
    marker_store = TrajectoryMarkerStore(tmp_path / "markers.jsonl")
    marker_store.append(TrajectoryMarker(
        source="codex",
        label="demo",
        phase="start",
        timestamp="2026-05-28T01:00:00.000Z",
        session="current",
    ))
    marker_store.append(TrajectoryMarker(
        source="codex",
        label="demo",
        phase="end",
        timestamp="2026-05-28T01:00:01.000Z",
        session="current",
    ))

    try:
        resolve_trajectory_slice(
            source="codex",
            session=str(session),
            marker_store_path=marker_store.path,
            label="demo",
        )
    except LookupError as exc:
        assert "demo" in str(exc)
    else:
        raise AssertionError("Expected explicit-session marker mismatch to be rejected")


def test_resolver_selects_codex_slice_from_exact_session_marker(tmp_path: Path):
    session = tmp_path / "codex.jsonl"
    _write_jsonl(session, [
        {
            "timestamp": "2026-05-28T01:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Exact task"},
        },
        {
            "timestamp": "2026-05-28T01:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Done"},
        },
    ])
    marker_store = TrajectoryMarkerStore(tmp_path / "markers.jsonl")
    marker_store.append(TrajectoryMarker(
        source="codex",
        label="demo",
        phase="start",
        timestamp="2026-05-28T01:00:00.000Z",
        session=str(session),
    ))
    marker_store.append(TrajectoryMarker(
        source="codex",
        label="demo",
        phase="end",
        timestamp="2026-05-28T01:00:01.000Z",
        session=str(session),
    ))

    slice_ = resolve_trajectory_slice(
        source="codex",
        session=str(session),
        marker_store_path=marker_store.path,
        label="demo",
    )

    assert slice_.user_messages == ["Exact task"]

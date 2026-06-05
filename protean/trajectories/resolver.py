"""Resolve trajectory sources and selected session slices."""

from __future__ import annotations

from pathlib import Path

from protean.trajectories.base import SessionAdapter, SessionSlice
from protean.trajectories.markers import TrajectoryMarkerStore

SUPPORTED_SOURCES = ("codex", "claude_code")


def trajectory_adapter_cls(source: str) -> type[SessionAdapter]:
    if source == "codex":
        from protean.trajectories.codex import CodexSessionAdapter

        return CodexSessionAdapter
    if source == "claude_code":
        from protean.trajectories.claude_code import ClaudeCodeSessionAdapter

        return ClaudeCodeSessionAdapter
    raise ValueError(
        f"Unsupported trajectory source: {source!r}. "
        f"Currently supported: {', '.join(SUPPORTED_SOURCES)}."
    )


def resolve_trajectory_slice(
    *,
    source: str,
    session: str,
    marker_store_path: Path,
    label: str = "",
    from_message: str = "",
    to_message: str = "",
    from_time: str = "",
    to_time: str = "",
) -> SessionSlice:
    adapter_cls = trajectory_adapter_cls(source)
    session_path = adapter_cls.resolve_session(session)
    adapter = adapter_cls(session_path)

    if label:
        marker_store = TrajectoryMarkerStore(marker_store_path)
        marker_session = str(session_path)
        start, end = marker_store.latest_pair(
            source=source,
            label=label,
            session=marker_session,
        )
        return adapter.slice(start_time=start.timestamp, end_time=end.timestamp)

    if from_message:
        return adapter.slice_by_messages(
            from_message=from_message,
            to_message=to_message,
        )

    if from_time and to_time:
        return adapter.slice(start_time=from_time, end_time=to_time)

    raise ValueError(
        "No trajectory range selected. Provide --label, --from-message, "
        "or both --from-time and --to-time."
    )

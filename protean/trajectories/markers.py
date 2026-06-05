"""Explicit trajectory episode markers stored by Protean."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Phase = Literal["start", "end"]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TrajectoryMarker:
    source: str
    label: str
    phase: Phase
    timestamp: str
    cwd: str = ""
    task: str = ""
    session: str = ""

    @classmethod
    def from_json(cls, line: str) -> "TrajectoryMarker":
        data = json.loads(line)
        return cls(
            source=str(data.get("source", "")),
            label=str(data.get("label", "")),
            phase=data.get("phase", "start"),
            timestamp=str(data.get("timestamp", "")),
            cwd=str(data.get("cwd", "")),
            task=str(data.get("task", "")),
            session=str(data.get("session", "")),
        )


class TrajectoryMarkerStore:
    """Append-only marker log under the Protean data directory."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def append(self, marker: TrajectoryMarker) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(marker), ensure_ascii=False) + "\n")

    def list(self) -> list[TrajectoryMarker]:
        if not self._path.exists():
            return []
        markers: list[TrajectoryMarker] = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    markers.append(TrajectoryMarker.from_json(line))
        return markers

    def latest_pair(
        self,
        *,
        source: str,
        label: str,
        session: str = "",
    ) -> tuple[TrajectoryMarker, TrajectoryMarker]:
        """Return the latest start/end pair matching source, label, and session."""
        starts: list[TrajectoryMarker] = []
        latest: tuple[TrajectoryMarker, TrajectoryMarker] | None = None
        for marker in self.list():
            if marker.source != source or marker.label != label:
                continue
            if session and marker.session and marker.session != session:
                continue
            if marker.phase == "start":
                starts.append(marker)
                continue
            if marker.phase != "end" or not starts:
                continue
            start = starts[-1]
            if session and start.session and start.session != session:
                continue
            latest = (start, marker)
        if latest is None:
            raise LookupError(f"No complete marker pair for source={source!r}, label={label!r}")
        return latest

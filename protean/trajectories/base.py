"""Shared primitives for local agent-session trajectory adapters."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from protean.skills.evolve import TrajectoryAdapter
from protean.skills.runner import RunTrajectory

# Matches `/skills/<NAME>/SKILL.md` anywhere in a string. Both Codex
# (exec_command shelling out to cat/sed/head) and Claude Code (Read/Bash
# on the SKILL.md file) reveal which skill the agent opened by writing
# the path into the tool_args / tool_result text. <NAME> is constrained
# to skill-directory-safe characters so we don't pick up arbitrary URLs.
_SKILL_PATH_RE = re.compile(r"/skills/(?:\.system/)?([A-Za-z0-9][A-Za-z0-9._-]*)/SKILL\.md")


def parse_time(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def stringify_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(part for part in parts if part)
    return json.dumps(value, ensure_ascii=False)


@dataclass
class CallRecord:
    call_id: str
    timestamp: str
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    result_timestamp: str = ""


@dataclass
class SessionSlice:
    source: str
    session_path: Path
    start_time: datetime
    end_time: datetime
    events: list[dict[str, Any]]
    react_events: list[dict[str, Any]]

    @property
    def user_messages(self) -> list[str]:
        return [
            str(event.get("message", ""))
            for event in self.react_events
            if event.get("type") == "message" and event.get("role") == "user"
        ]

    @property
    def tool_count(self) -> int:
        return sum(1 for event in self.react_events if event.get("type") == "tool_call")

    @property
    def used_skills(self) -> list[str]:
        """Skill names referenced via SKILL.md paths in tool args or results.

        Detects skill usage across Codex (shell commands reading SKILL.md)
        and Claude Code (Read/Bash on SKILL.md) by scanning tool_call /
        tool_result text for `/skills/<name>/SKILL.md` paths. Order
        preserved, deduplicated. The Protean bootstrap meta-skill is
        excluded — the agent reads it on every Protean-driven episode, so
        it's a constant signal that adds no router value and could
        mislead the router into "refining" a builtin.
        """
        from protean.skills.bootstrap import AGENT_PROTEAN_SKILL_NAME

        seen: dict[str, None] = {}
        for event in self.react_events:
            etype = event.get("type")
            if etype == "tool_call":
                blob = json.dumps(event.get("tool_args") or {}, ensure_ascii=False)
            elif etype == "tool_result":
                blob = str(event.get("result") or "")
            else:
                continue
            for match in _SKILL_PATH_RE.finditer(blob):
                name = match.group(1)
                if name == AGENT_PROTEAN_SKILL_NAME:
                    continue
                seen.setdefault(name, None)
        return list(seen)

    def to_run_trajectory(self, *, task_name: str = "", verify_reason: str = "") -> RunTrajectory:
        actions, instruction, final_response = TrajectoryAdapter.from_react(self.react_events)
        return TrajectoryAdapter.to_run_trajectory(
            actions,
            instruction,
            final_response,
            task_name=task_name or f"{self.source}-session",
            verify_reason=verify_reason,
        )


class SessionAdapter(Protocol):
    source: str
    session_path: Path

    @staticmethod
    def resolve_session(session: str = "current") -> Path: ...

    def slice(self, *, start_time: str | datetime, end_time: str | datetime) -> SessionSlice: ...

    def slice_by_messages(self, *, from_message: str, to_message: str = "") -> SessionSlice: ...


class JsonlSessionAdapter:
    """Base class for one-file JSONL agent session adapters.

    Subclasses own runtime-specific session discovery and schema parsing. This
    base owns range selection, message selector matching, and conversion into a
    provider-neutral ``SessionSlice``.
    """

    source = "jsonl"

    def __init__(self, session_path: Path) -> None:
        self.session_path = session_path
        self.events = read_jsonl(session_path)

    def slice(
        self,
        *,
        start_time: str | datetime,
        end_time: str | datetime,
    ) -> SessionSlice:
        start = parse_time(start_time) if isinstance(start_time, str) else start_time
        end = parse_time(end_time) if isinstance(end_time, str) else end_time
        if end < start:
            raise ValueError("end_time must be after start_time")
        events: list[dict[str, Any]] = []
        for event in self.events:
            try:
                event_time = self._event_time(event)
            except ValueError:
                continue
            if start <= event_time <= end:
                events.append(event)
        if not events:
            raise LookupError(f"No {self.source} events found in selected range")
        return SessionSlice(
            source=self.source,
            session_path=self.session_path,
            start_time=start,
            end_time=end,
            events=events,
            react_events=self.to_react_events(events),
        )

    def slice_by_messages(
        self,
        *,
        from_message: str,
        to_message: str = "",
    ) -> SessionSlice:
        start = self.find_message_time(from_message)
        end = self.find_message_time(to_message, after=start) if to_message else self._last_time()
        return self.slice(start_time=start, end_time=end)

    def find_message_time(self, needle: str, *, after: datetime | None = None) -> datetime:
        target = needle.strip()
        if not target:
            raise ValueError("message selector is empty")
        for event in self.events:
            try:
                event_time = self._event_time(event)
            except ValueError:
                continue
            if after is not None and event_time <= after:
                continue
            message = self.message_text(event)
            if message is None:
                continue
            _, text = message
            if target in text:
                return event_time
        raise LookupError(f"Message selector not found in {self.source} session: {needle!r}")

    def message_text(self, event: dict[str, Any]) -> tuple[str, str] | None:
        raise NotImplementedError

    def to_react_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _event_time(self, event: dict[str, Any]) -> datetime:
        return parse_time(str(event.get("timestamp") or ""))

    def _last_time(self) -> datetime:
        for event in reversed(self.events):
            try:
                return self._event_time(event)
            except ValueError:
                continue
        raise LookupError(f"{self.source} session has no timestamped events")

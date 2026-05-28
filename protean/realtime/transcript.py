"""Transcript — records a teaching session's conversation and tool trace.

Captures everything needed for post-session skill generation:
  - User speech (transcribed text + duration)
  - Assistant speech (text)
  - Tool calls and results
  - Timestamps
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from protean.channels.realtime_events import ToolCallRequest


@dataclass
class TranscriptEntry:
    """A single entry in the session transcript."""

    timestamp: float
    role: Literal["user", "assistant", "tool_call", "tool_result"]
    content: str = ""
    audio_duration: float | None = None
    tool_call: ToolCallRequest | None = None
    tool_result: str | None = None


class Transcript:
    """Records a complete teaching session transcript."""

    def __init__(self) -> None:
        self.entries: list[TranscriptEntry] = []
        self._start_time = time.monotonic()

    def _ts(self) -> float:
        return time.monotonic() - self._start_time

    def add_user_text(self, text: str, audio_duration: float | None = None) -> None:
        self.entries.append(
            TranscriptEntry(
                timestamp=self._ts(),
                role="user",
                content=text,
                audio_duration=audio_duration,
            )
        )

    def add_assistant_text(self, text: str) -> None:
        self.entries.append(
            TranscriptEntry(
                timestamp=self._ts(),
                role="assistant",
                content=text,
            )
        )

    def add_tool_call(self, call: ToolCallRequest) -> None:
        self.entries.append(
            TranscriptEntry(
                timestamp=self._ts(),
                role="tool_call",
                content=f"{call.name}({call.arguments})",
                tool_call=call,
            )
        )

    def add_tool_result(self, call_id: str, result: str) -> None:
        self.entries.append(
            TranscriptEntry(
                timestamp=self._ts(),
                role="tool_result",
                content=result,
                tool_result=result,
            )
        )

    def to_evidence_text(self) -> str:
        """Export as plain text for skill generator input."""
        lines = []
        for e in self.entries:
            ts = f"[{e.timestamp:.1f}s]"
            if e.role == "user":
                lines.append(f"{ts} User: {e.content}")
            elif e.role == "assistant":
                lines.append(f"{ts} Assistant: {e.content}")
            elif e.role == "tool_call":
                lines.append(f"{ts} Tool call: {e.content}")
            elif e.role == "tool_result":
                lines.append(f"{ts} Tool result: {e.content}")
        return "\n".join(lines)

    def last_voice_rounds(self, n: int) -> list[tuple[str, str]]:
        """Return up to the last `n` rounds of user/assistant voice turns.

        A round is one user or assistant entry; tool_call / tool_result
        entries are skipped. Returned in chronological order as
        (role, content) tuples, where role is "User" or "Assistant".
        """
        if n <= 0:
            return []
        collected: list[tuple[str, str]] = []
        for e in reversed(self.entries):
            if e.role not in ("user", "assistant"):
                continue
            label = "User" if e.role == "user" else "Assistant"
            collected.append((label, e.content))
            if len(collected) >= n:
                break
        collected.reverse()
        return collected

    @property
    def duration(self) -> float:
        if not self.entries:
            return 0.0
        return self.entries[-1].timestamp

    @property
    def tool_calls(self) -> list[TranscriptEntry]:
        return [e for e in self.entries if e.role == "tool_call"]

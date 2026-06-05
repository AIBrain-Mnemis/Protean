"""Claude Code local session JSONL adapter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from protean.trajectories.base import JsonlSessionAdapter, stringify_content


def _claude_home() -> Path:
    return Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude")))


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


class ClaudeCodeSessionAdapter(JsonlSessionAdapter):
    """Read one Claude Code session file and convert a selected slice to ReAct events."""

    source = "claude_code"

    @staticmethod
    def resolve_session(session: str = "current") -> Path:
        """Resolve the current Claude Code session path.

        The adapter is intentionally single-session: explicit session path,
        environment-provided path, or newest local Claude Code JSONL file.
        """
        if session and session != "current":
            path = Path(session).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"Claude Code session not found: {path}")
            return path

        env_path = os.getenv("CLAUDE_SESSION_FILE") or os.getenv("CLAUDE_SESSION_PATH")
        if env_path:
            path = Path(env_path).expanduser()
            if path.exists():
                return path
            raise FileNotFoundError(f"Claude Code session from environment not found: {path}")

        sessions_root = _claude_home() / "projects"
        candidates = sorted(
            sessions_root.glob("**/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise FileNotFoundError(
                f"No Claude Code session JSONL files found under {sessions_root}"
            )
        return candidates[0]

    def message_text(self, event: dict[str, Any]) -> tuple[str, str] | None:
        if event.get("type") not in {"user", "assistant"}:
            return None
        message = event.get("message") or {}
        if not isinstance(message, dict):
            return None
        role = str(message.get("role") or event.get("type") or "")
        text = _message_content_text(message.get("content"))
        return (role, text) if text else None

    def to_react_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        react_events: list[dict[str, Any]] = []
        tool_names: dict[str, str] = {}
        seen_calls: set[str] = set()
        seen_results: set[str] = set()

        for event in events:
            if event.get("type") not in {"user", "assistant"}:
                continue
            timestamp = str(event.get("timestamp") or "")
            message = event.get("message") or {}
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or event.get("type") or "")
            content = message.get("content")

            if isinstance(content, str):
                if content:
                    react_events.append({
                        "type": "message",
                        "role": role,
                        "message": content,
                        "timestamp": timestamp,
                    })
                continue

            if not isinstance(content, list):
                continue

            pending_text: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "text" and isinstance(item.get("text"), str):
                    pending_text.append(item["text"])
                    continue

                if pending_text:
                    react_events.append({
                        "type": "message",
                        "role": role,
                        "message": "\n".join(pending_text),
                        "timestamp": timestamp,
                    })
                    pending_text = []

                if item_type == "tool_use":
                    call_id = str(item.get("id") or "")
                    if not call_id or call_id in seen_calls:
                        continue
                    tool_name = str(item.get("name") or "")
                    tool_args = item.get("input") if isinstance(item.get("input"), dict) else {}
                    tool_names[call_id] = tool_name
                    seen_calls.add(call_id)
                    react_events.append({
                        "type": "tool_call",
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "timestamp": timestamp,
                    })
                    continue

                if item_type == "tool_result":
                    call_id = str(item.get("tool_use_id") or "")
                    if not call_id or call_id in seen_results:
                        continue
                    seen_results.add(call_id)
                    react_events.append({
                        "type": "tool_result",
                        "call_id": call_id,
                        "tool_name": tool_names.get(call_id, ""),
                        "result": stringify_content(item.get("content")),
                        "timestamp": timestamp,
                    })

            if pending_text:
                react_events.append({
                    "type": "message",
                    "role": role,
                    "message": "\n".join(pending_text),
                    "timestamp": timestamp,
                })

        return react_events

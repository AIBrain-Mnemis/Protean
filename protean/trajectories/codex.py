"""Codex local session JSONL adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from protean.trajectories.base import (
    CallRecord,
    JsonlSessionAdapter,
    extract_images,
    stringify_content,
)


def _codex_home() -> Path:
    return Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))


def _assign_image_roles(
    raw: list[tuple[bytes, str]], from_protean_mcp: bool,
) -> list[tuple[bytes, str, str]]:
    """Assign overview/detail roles based on source.

    Protean MCP result_to_mcp outputs: screenshot (overview) then
    optional detail_crop (detail). Other sources: all overview.
    """
    if not from_protean_mcp:
        return [(data, mime, "overview") for data, mime in raw]
    result: list[tuple[bytes, str, str]] = []
    for i, (data, mime) in enumerate(raw):
        role = "detail" if i == 1 else "overview"
        result.append((data, mime, role))
    return result


def _loads_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return data if isinstance(data, dict) else {"value": data}
    return {}


class CodexSessionAdapter(JsonlSessionAdapter):
    """Read one Codex session file and convert a selected slice to ReAct events."""

    source = "codex"

    @staticmethod
    def resolve_session(session: str = "current") -> Path:
        """Resolve the current Codex session path.

        Agent runtimes should pass an explicit session path when they know it.
        For Codex Desktop, ``current`` falls back to the newest session JSONL.
        The adapter never merges sessions.
        """
        if session and session != "current":
            path = Path(session).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"Codex session not found: {path}")
            return path

        env_path = os.getenv("CODEX_SESSION_FILE") or os.getenv("CODEX_SESSION_PATH")
        if env_path:
            path = Path(env_path).expanduser()
            if path.exists():
                return path
            raise FileNotFoundError(f"Codex session from environment not found: {path}")

        sessions_root = _codex_home() / "sessions"
        candidates = sorted(
            sessions_root.glob("**/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise FileNotFoundError(f"No Codex session JSONL files found under {sessions_root}")
        return candidates[0]

    def message_text(self, event: dict[str, Any]) -> tuple[str, str] | None:
        payload = event.get("payload") or {}
        if event.get("type") != "event_msg":
            return None
        ptype = payload.get("type")
        if ptype not in {"user_message", "agent_message"}:
            return None
        role = "user" if ptype == "user_message" else "assistant"
        message = str(payload.get("message") or "")
        return (role, message) if message else None

    def to_react_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        calls: dict[str, CallRecord] = {}

        for event in events:
            payload = event.get("payload") or {}
            timestamp = str(event.get("timestamp") or "")
            etype = event.get("type")
            ptype = payload.get("type")

            if etype == "event_msg" and ptype in {"user_message", "agent_message"}:
                role = "user" if ptype == "user_message" else "assistant"
                message = str(payload.get("message") or "")
                if message:
                    messages.append({
                        "type": "message",
                        "role": role,
                        "message": message,
                        "timestamp": timestamp,
                    })
                continue

            if etype == "response_item" and ptype == "function_call":
                call_id = str(payload.get("call_id") or "")
                if not call_id:
                    continue
                record = calls.setdefault(call_id, CallRecord(call_id, timestamp))
                record.timestamp = record.timestamp or timestamp
                record.tool_name = str(payload.get("name") or record.tool_name)
                args = _loads_args(payload.get("arguments"))
                if args:
                    record.tool_args = args
                continue

            if etype == "response_item" and ptype == "function_call_output":
                call_id = str(payload.get("call_id") or "")
                if not call_id:
                    continue
                record = calls.setdefault(call_id, CallRecord(call_id, timestamp))
                if not record.result:
                    output = payload.get("output")
                    record.result = stringify_content(output)
                    record.images = _assign_image_roles(
                        extract_images(output), record.from_mcp,
                    )
                    record.result_timestamp = timestamp
                continue

            if etype == "event_msg" and ptype == "mcp_tool_call_end":
                call_id = str(payload.get("call_id") or "")
                if not call_id:
                    continue
                record = calls.setdefault(call_id, CallRecord(call_id, timestamp))
                invocation = payload.get("invocation") or {}
                record.tool_name = str(invocation.get("tool") or record.tool_name)
                args = invocation.get("arguments")
                if isinstance(args, dict):
                    record.tool_args = args
                record.from_mcp = invocation.get("server") == "protean"
                result = payload.get("result")
                if result is not None:
                    record.result = stringify_content(result)
                    record.images = _assign_image_roles(
                        extract_images(result), record.from_mcp,
                    )
                    record.result_timestamp = timestamp

        combined: list[dict[str, Any]] = list(messages)
        for record in calls.values():
            if record.tool_name:
                combined.append({
                    "type": "tool_call",
                    "call_id": record.call_id,
                    "tool_name": record.tool_name,
                    "tool_args": record.tool_args,
                    "timestamp": record.timestamp,
                })
            if record.result:
                event: dict[str, Any] = {
                    "type": "tool_result",
                    "call_id": record.call_id,
                    "tool_name": record.tool_name,
                    "result": record.result,
                    "timestamp": record.result_timestamp or record.timestamp,
                }
                if record.images:
                    event["images"] = record.images
                combined.append(event)

        return sorted(combined, key=lambda item: item.get("timestamp", ""))

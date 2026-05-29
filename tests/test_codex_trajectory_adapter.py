from __future__ import annotations

import json
from pathlib import Path

from protean.trajectories.codex import CodexSessionAdapter


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_codex_adapter_dedups_tool_results_by_call_id(tmp_path: Path):
    session = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-05-28T01:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Book a meeting"},
        },
        {
            "timestamp": "2026-05-28T01:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "activate_app",
                "arguments": json.dumps({"app": "Microsoft Outlook"}),
                "call_id": "call_1",
            },
        },
        {
            "timestamp": "2026-05-28T01:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "generic output",
            },
        },
        {
            "timestamp": "2026-05-28T01:00:03.000Z",
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "call_1",
                "invocation": {
                    "server": "protean",
                    "tool": "activate_app",
                    "arguments": {"app": "Microsoft Outlook"},
                },
                "result": "Activated Microsoft Outlook",
            },
        },
        {
            "timestamp": "2026-05-28T01:00:04.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Done"},
        },
    ]
    _write_jsonl(session, rows)

    adapter = CodexSessionAdapter(session)
    slice_ = adapter.slice_by_messages(from_message="Book a meeting")

    tool_calls = [event for event in slice_.react_events if event["type"] == "tool_call"]
    tool_results = [event for event in slice_.react_events if event["type"] == "tool_result"]
    assert len(tool_calls) == 1
    assert len(tool_results) == 1
    assert tool_results[0]["result"] == "Activated Microsoft Outlook"

    trajectory = slice_.to_run_trajectory(task_name="book-meeting")
    actions = trajectory.steps[0].actions
    assert trajectory.task == "Book a meeting"
    assert [a.event_type for a in actions].count("tool_call") == 1
    assert [a.event_type for a in actions].count("tool_result") == 1


def test_codex_adapter_rejects_missing_message_selector(tmp_path: Path):
    session = tmp_path / "rollout.jsonl"
    _write_jsonl(session, [
        {
            "timestamp": "2026-05-28T01:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Different task"},
        }
    ])

    adapter = CodexSessionAdapter(session)
    try:
        adapter.slice_by_messages(from_message="Book a meeting")
    except LookupError as exc:
        assert "Book a meeting" in str(exc)
    else:
        raise AssertionError("Expected missing message selector to be rejected")

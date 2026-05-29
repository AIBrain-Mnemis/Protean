from __future__ import annotations

import json
from pathlib import Path

from protean.trajectories.claude_code import ClaudeCodeSessionAdapter


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_claude_code_adapter_converts_messages_tools_and_results(tmp_path: Path):
    session = tmp_path / "claude.jsonl"
    rows = [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": "s1"},
        {
            "timestamp": "2026-05-28T01:00:00.000Z",
            "type": "user",
            "message": {"role": "user", "content": "Inspect the repo"},
        },
        {
            "timestamp": "2026-05-28T01:00:01.000Z",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will inspect the tree."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
        },
        {
            "timestamp": "2026-05-28T01:00:02.000Z",
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "protean\ntests",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "duplicate should be ignored",
                    },
                ],
            },
            "toolUseResult": {"stdout": "protean\ntests"},
        },
        {
            "timestamp": "2026-05-28T01:00:03.000Z",
            "type": "assistant",
            "message": {"role": "assistant", "content": "Done"},
        },
    ]
    _write_jsonl(session, rows)

    adapter = ClaudeCodeSessionAdapter(session)
    slice_ = adapter.slice_by_messages(from_message="Inspect the repo")

    assert slice_.source == "claude_code"
    assert slice_.user_messages == ["Inspect the repo"]

    tool_calls = [event for event in slice_.react_events if event["type"] == "tool_call"]
    tool_results = [event for event in slice_.react_events if event["type"] == "tool_result"]
    assert tool_calls == [{
        "type": "tool_call",
        "call_id": "toolu_1",
        "tool_name": "Bash",
        "tool_args": {"command": "ls"},
        "timestamp": "2026-05-28T01:00:01.000Z",
    }]
    assert len(tool_results) == 1
    assert tool_results[0]["call_id"] == "toolu_1"
    assert tool_results[0]["tool_name"] == "Bash"
    assert tool_results[0]["result"] == "protean\ntests"

    trajectory = slice_.to_run_trajectory(task_name="inspect-repo")
    actions = trajectory.steps[0].actions
    assert trajectory.task == "Inspect the repo"
    assert trajectory.skill_name == "inspect-repo"
    assert [a.event_type for a in actions].count("tool_call") == 1
    assert [a.event_type for a in actions].count("tool_result") == 1


def test_claude_code_adapter_rejects_missing_message_selector(tmp_path: Path):
    session = tmp_path / "claude.jsonl"
    _write_jsonl(session, [
        {
            "timestamp": "2026-05-28T01:00:00.000Z",
            "type": "user",
            "message": {"role": "user", "content": "Different task"},
        }
    ])

    adapter = ClaudeCodeSessionAdapter(session)
    try:
        adapter.slice_by_messages(from_message="Inspect the repo")
    except LookupError as exc:
        assert "Inspect the repo" in str(exc)
    else:
        raise AssertionError("Expected missing message selector to be rejected")

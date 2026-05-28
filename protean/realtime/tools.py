"""Tool declarations for realtime LLM (Talker) function calling.

The Talker has high-level tools that map to SkillBuilder operations
and task execution. It does NOT have low-level GUI tools (click, type) —
those are used by the Executor internally.
"""

from __future__ import annotations


def build_realtime_tool_declarations() -> list[dict]:
    """Build function declarations for the Talker's realtime LLM.

    Tools:
      - observe_step: record a step the user demonstrated
      - execute_step: execute a step on the user's behalf
      - revise_step: revise a previous step based on feedback
      - remove_step: remove a step
      - finalize_skill: finish and generate the final skill
    - replay_skill: replay a finalized skill revision
      - interrupt_executor: stop current execution
      - start_screen / stop_screen: screen sharing modes
      - set_task: set the current task description
      - request_screenshot: take a fresh screenshot
      - check_executor: check executor progress
    """
    return [
        {
            "name": "observe_step",
            "handler": "python",
            "description": (
                "Record a step the user just demonstrated on screen. "
                "Call this each time the user completes a single UI interaction "
                "that changes the screen state — e.g. a click, a form fill, "
                "a menu selection, or a keyboard shortcut."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": (
                            "What this step achieves — e.g. 'Open the new meeting composer'."
                        ),
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "How the step was performed — e.g. 'Click New Meeting in "
                            "the Outlook calendar toolbar'."
                        ),
                    },
                },
                "required": ["intent", "action"],
            },
        },
        {
            "name": "execute_step",
            "handler": "python",
            "description": "Execute a step the user just requested.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_request": {
                        "type": "string",
                        "description": (
                            "The user's spoken request, kept faithful to "
                            "what they actually said. Don't paraphrase, "
                            "summarize, or generalize the verb. Preserve "
                            "surrounding context the user gave (prior "
                            "requests, constraints, clarifications, "
                            "corrections) when it's needed to make the "
                            "request unambiguous. Use on-screen UI labels "
                            "verbatim."
                        ),
                    },
                },
                "required": ["user_request"],
            },
        },
        {
            "name": "revise_step",
            "handler": "python",
            "description": (
                "Revise a step in the current editable draft. After replay_skill, this "
                "draft comes from the loaded finalized skill revision."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "step_index": {
                        "type": "integer",
                        "description": "0-based index of the step to revise.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Updated step intent: what this step achieves (optional).",
                    },
                    "action": {
                        "type": "string",
                        "description": "Updated step action: how to perform the step (optional).",
                    },
                    "feedback": {
                        "type": "string",
                        "description": "User's feedback about this step.",
                    },
                },
                "required": ["step_index"],
            },
        },
        {
            "name": "remove_step",
            "handler": "python",
            "description": "Remove a step from the skill draft.",
            "parameters": {
                "type": "object",
                "properties": {
                    "step_index": {
                        "type": "integer",
                        "description": "0-based index of the step to remove.",
                    },
                },
                "required": ["step_index"],
            },
        },
        {
            "name": "finalize_skill",
            "handler": "python",
            "description": (
                "Finish editing the current draft and generate a finalized skill revision. "
                "Call when the user says they are done teaching or done refining."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "replay_skill",
            "handler": "python",
            "description": (
                "Replay a finalized skill end-to-end by rendering its full SKILL.md and sending "
                "the whole workflow to the executor in a fresh executor session. Always provide "
                "the finalized skill name. Replay also loads that skill as the current editable "
                "draft so the user can revise steps afterward. Use execute_step instead when the "
                "workflow is still being discovered."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Finalized skill name to replay. Required."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "interrupt_executor",
            "handler": "python",
            "description": (
                "Stop the currently running execution or replay. "
                "Use only when the user says stop, cancel, or wait."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "start_screen",
            "handler": "bridge",
            "description": (
                "Start screen handling in one of two modes. "
                "Use mode='observe' when the user wants you to watch their "
                "screen, such as when they are sharing, showing, or "
                "demonstrating something. Use mode='share' when the user wants "
                "to watch your screen while you act. Do not use mode='share' "
                "while the user is still sharing. If the user's intent is "
                "ambiguous, ask a clarification question instead of guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["observe", "share"],
                        "description": (
                            "'observe' when the user wants you to watch their screen, "
                            "or 'share' when they want to watch your screen."
                        ),
                    },
                    "display": {
                        "type": "integer",
                        "description": (
                            "Display index (1-based) to share when mode='share'. "
                            "Defaults to 1 (primary display). Ignored for mode='observe'."
                        ),
                    },
                },
                "required": ["mode"],
            },
        },
        {
            "name": "stop_screen",
            "handler": "bridge",
            "description": (
                "Stop the current screen mode, whether you were observing the "
                "user's shared screen or sharing your own screen."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "set_task",
            "handler": "python",
            "description": (
                "Set or update the current task. Call whenever the user "
                "clarifies or changes what they want to accomplish."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Brief description of the task, e.g. "
                            "'Create a meeting in Outlook'."
                        ),
                    },
                },
                "required": ["task"],
            },
        },
        {
            "name": "request_screenshot",
            "handler": "bridge",
            "description": (
                "Take a fresh screenshot and send it to you. "
                "Use when the user asks you to look at something specific, "
                "or when you need to confirm what is currently on screen."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "check_executor",
            "handler": "python",
            "description": (
                "Check what the executor is currently doing. "
                "Use when the user asks about progress."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    ]

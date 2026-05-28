"""Computer Use executor — drives GUI via tool-calling agentic loop.

The model sees screenshots as inline images and calls standard function tools
(screenshot, left_click, type_text, key_press, scroll, etc.) to drive the GUI.
Each iteration: model sees screen → decides action → we execute → take screenshot → loop.

This avoids the Claude Code proxy limitation where computer_20250124 tool inputs
get stripped, by using normal function-call tools that the proxy handles correctly.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import TYPE_CHECKING, Any, AsyncIterator

from PIL import Image

from protean.config import DEFAULT_MAX_TOKENS
from protean.executor import ExecutorContext, ExecutorEvent, ExecutorEventType, ExecutorProvider
from protean.llm import create_sync_client
from protean.platform.base import (
    LLM_JPEG_QUALITY,
    LLM_SCREENSHOT_HEIGHT,
    LLM_SCREENSHOT_WIDTH,
    CoordinateMapper,
    parse_key_combo,
)

if TYPE_CHECKING:
    from protean.platform.base import Platform

log = logging.getLogger(__name__)

# Screenshots are resized to this resolution before sending to the model.
# TEMPORARY: 1024x768 is 4:3, but virtually every real display (and OSWorld VM
# defaults) is 16:9 / 16:10. _DisplayScale below does *non-uniform* scaling, so
# the LLM sees a horizontally-squashed image and the geometry of UI controls
# (button shape, icon proportions) is distorted before inference. This likely
# costs a measurable amount of click accuracy on small / round targets.
# Proper fix: pick an API resolution with the same aspect ratio as the actual
# display (e.g. 1280x720 for 16:9, 1280x800 for 16:10) and reject mixed-ratio
# scaling in _DisplayScale, or downscale uniformly + letterbox.
# Also note: the strings "1024x768", "0-1023", "0-767" are hard-coded in tool
# descriptions and the system prompt below — when this is fixed, those must
# be templated off these constants too.
_DEFAULT_DISPLAY_WIDTH = 1024
_DEFAULT_DISPLAY_HEIGHT = 768
_JPEG_QUALITY = 70
_MAX_ITERATIONS = 500
_REASONING_TRUNCATE = 500
# Pixel spacing of the coordinate grid drawn on the detail-crop view.
_DETAIL_GRID_STEP = 100


# ── Tool definitions (standard function tools) ──────────────

_BASE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "screenshot",
        "description": (
            "Take a screenshot of the current screen. Returns the image. "
            "Call this first to see what's on screen before acting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "left_click",
        "description": (
            "Click the left mouse button at the given (x, y) pixel coordinates. "
            "Coordinates are relative to the screenshot image (1024x768)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate (0-1023)"},
                "y": {"type": "integer", "description": "Y coordinate (0-767)"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "right_click",
        "description": "Right-click at the given (x, y) pixel coordinates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "double_click",
        "description": "Double-click the left mouse button at (x, y).",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "mouse_move",
        "description": "Move the mouse cursor to (x, y) without clicking.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type the given text string. The text is typed character by character "
            "into whatever field currently has focus. Use left_click first to focus "
            "the target input field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "key_press",
        "description": (
            "Press a key or key combination. Examples: 'return', 'tab', 'escape', "
            "'cmd+c', 'cmd+v', 'cmd+a', 'ctrl+c', 'alt+tab', 'shift+tab', "
            "'cmd+shift+n', 'up', 'down', 'left', 'right', 'backspace', 'delete', "
            "'space', 'cmd+w', 'cmd+q'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "Key or combo, e.g. 'return', 'cmd+c', 'alt+tab'",
                },
            },
            "required": ["keys"],
        },
    },
    {
        "name": "scroll",
        "description": (
            "Scroll at the given (x, y) position. Direction can be 'up', 'down', "
            "'left', or 'right'. Amount is the number of scroll steps (default 3)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate to scroll at"},
                "y": {"type": "integer", "description": "Y coordinate to scroll at"},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "description": "Scroll direction",
                },
                "amount": {
                    "type": "integer",
                    "description": "Number of scroll steps (default 3)",
                },
            },
            "required": ["x", "y", "direction"],
        },
    },
    {
        "name": "wait",
        "description": "Wait for the given number of seconds (e.g. for loading).",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Seconds to wait (default 2)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "done",
        "description": (
            "Call this when the task is complete. Include a brief summary of "
            "what was accomplished."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was done",
                },
            },
            "required": ["summary"],
        },
    },
]

_TOOLS_BY_NAME: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in _BASE_TOOLS}


# Optional terminal tool — appended to per-instance tool list when
# ComputerUseExecutor is constructed with enable_terminal=True. Backed by
# the DesktopCommanderMCP server (https://github.com/wonderwhy-er/DesktopCommanderMCP)
# launched over stdio.
_TERMINAL_TOOL: dict[str, Any] = {
    "name": "run_terminal_command",
    "description": (
        "Run a shell command in a hidden background process. The command "
        "starts immediately and you get back its PID plus any initial "
        "output. The process keeps running — you will be notified "
        "automatically when it finishes, goes idle, or needs interactive "
        "input. Use this whenever the task is to READ information from the "
        "system (list files, check a path exists, read a log, get a version, "
        "query a service). Examples: 'ls -la /tmp', 'systemctl status nginx', "
        "'git status'. "
        "Each call starts a fresh shell — no state persists between calls. "
        "If a process needs interactive input (Password:, [Y/n]), use "
        "send_terminal_input(pid, input) to respond. "
        "Output per message is truncated at ~8 KB."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The shell command to run, "
                    "e.g. 'dir D:\\\\Logs' or 'systemctl status nginx'"
                ),
            },
            "timeout_seconds": {
                "type": "number",
                "description": (
                    "How long to wait for the process to complete before "
                    "returning control to you. If the process finishes within "
                    "this time, you get the full output directly. If not, you "
                    "get the initial output and a PID — the process continues "
                    "in background and you'll be notified when it finishes. "
                    "Default 30. Use a short timeout (e.g. 1-5) for commands "
                    "you know will take long."
                ),
            },
            "shell": {
                "type": "string",
                "description": (
                    "Override shell, e.g. 'powershell.exe', 'cmd.exe', "
                    "'/bin/bash'. Defaults to system shell."
                ),
            },
        },
        "required": ["command"],
    },
}

_SEND_INPUT_TOOL: dict[str, Any] = {
    "name": "send_terminal_input",
    "description": (
        "Send input (stdin) to a running background process by PID. "
        "Use this to answer interactive prompts (Password:, [Y/n], etc.) "
        "from processes started with run_terminal_command. Returns the "
        "process response text and a screenshot."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "description": "PID of the process to send input to.",
            },
            "input": {
                "type": "string",
                "description": (
                    "Text to send to the process stdin."
                ),
            },
        },
        "required": ["pid", "input"],
    },
}

_TERMINAL_PROMPT_ADDENDUM = """\

TERMINAL ACCESS — READ THIS BEFORE EXECUTING ANY SKILL:

You have two terminal tools:

1. `run_terminal_command(command, timeout_seconds?, shell?)` — runs a shell
   command in a hidden background process. The command starts immediately
   and you get back its PID plus any initial output. The process keeps running
   in the background — you will be NOTIFIED automatically when it:
     - finishes (with exit code and output)
     - goes idle (no output for ~30s — may be hung or waiting for input)
     - is waiting for interactive input (e.g. Password:, [Y/n])
     - produces too much output (terminated to prevent disk fill)

2. `send_terminal_input(pid, input)` — sends text to a running process's
   stdin. Use this to respond to interactive prompts (Password:, [Y/n],
   etc.). A newline (Enter) is appended automatically, e.g. input="y".

There is NO visible terminal window — nothing appears on the user's screen.
However, a screenshot is included with every result, because some commands
may trigger visual popups (e.g. permission dialogs, Keychain auth).

DECIDE BEFORE ACTING:
  - If the user's actual goal is to READ information from the system
    (list files / check status / read logs / get version / inspect
    config), you MUST use run_terminal_command and skip the GUI flow.
    Example: task "list files in /tmp" → just call
      run_terminal_command(command="ls -la /tmp")
    and report the output. Do NOT open a terminal app or any GUI
    even if the SKILL.md describes that flow.
  - If the goal genuinely requires GUI interaction (sending a Teams
    message, scheduling an Outlook meeting, clicking a Settings
    toggle), follow the SKILL.md GUI steps as usual.
  - For long-running commands (builds, installs, large downloads),
    run_terminal_command is fine — the process runs in background and
    you'll be notified when it completes. You can continue doing other
    work while waiting.
  - When a task involves repetitive operations on multiple items
    (processing rows in a spreadsheet, batch renaming files, checking
    rules across entries), write a script and run it via
    run_terminal_command instead of repeating GUI actions one by one.

When in doubt: if the answer to the user's question is text that a
shell command would print, use run_terminal_command.
"""


# Default command for spawning the DesktopCommanderMCP server.
_DEFAULT_MCP_TERMINAL_COMMAND: list[str] = [
    "npx", "-y", "@wonderwhy-er/desktop-commander@latest",
]

# Cap on text returned to the model from a single command. Output beyond this
# is truncated head+tail with a marker so the model still sees both ends.
_TERMINAL_OUTPUT_MAX_BYTES = 8192

# Cap on total accumulated output from a background process before we
# force-terminate it to prevent disk/memory fill.
_TERMINAL_MAX_OUTPUT_BYTES = 4 * 1024 * 1024  # 4 MB

# If the process produces no new output for this many consecutive polls,
# we notify the model that the process appears idle.
_TERMINAL_IDLE_POLLS_LIMIT = 15  # ~30s at 2s poll interval

# Patterns that suggest the process is waiting for interactive input.
_INTERACTIVE_PROMPT_RE = re.compile(
    r"(?i)"
    r"(?:password\s*[:\>])"
    r"|(?:\[Y/n\]|\[y/N\]|\(y/n\))"
    r"|(?:Are you sure.*\?)"
    r"|(?:Press (?:any key|ENTER|RETURN))"
    r"|(?:passphrase\s*[:\>])"
    r"|(?:yes/no\)?\s*[:\>]?\s*$)"
)


_SYSTEM_PROMPT = """\
You are a GUI automation agent. You interact with a computer using screenshots and action tools.

CORE RULES:
1. Always call `screenshot` first to observe the current UI state.
2. Every action tool (left_click, type_text, key_press, etc.) returns a new screenshot — do NOT call screenshot again immediately after an action.
3. Coordinate-based actions also return a **detail view**: a 2× zoomed crop around the action point with a coordinate grid overlay (100 px spacing). Use the detail view to verify you clicked the correct element and to read nearby coordinates precisely.
4. Coordinates are in screenshot space (1024x768):
   - (0, 0) is the top-left corner of the screen
   - x increases to the right
   - y increases downward
   - Click targets should be the visual center of UI elements
5. Carefully read all visible text and UI elements before acting.

INTERACTION RULES:
6. To type into a field, click it first to ensure focus.
7. Detect the operating system from the screenshot:
   - Windows → use `ctrl`
   - macOS → use `cmd`
8. To open applications:
   - Windows: press `key_press('win')`, type app name, then `key_press('return')`
   - macOS: press `key_press('cmd+space')`, type app name, then `key_press('return')`
   Wait 2-3 seconds for applications to load before interacting.

**ERROR HANDLING (CRITICAL)**:
9. Every coordinate-based tool (e.g. left_click) must follow this exact schema: {"x": <int>, "y": <int>}. Never use formats like "x, y", "(x, y)".
10. If a click does not produce the expected result (e.g. clicked a wrong element, close the window by mistake), explicitly follow below steps to self-correct in your reasoning and try again:
   a. Recall the exact coordinates you clicked (x, y).
   b. Identify what UI element was actually clicked at that location.
   c. Determine the spatial relation between clicked point and target:
      - target is LEFT / RIGHT / ABOVE / BELOW relative to clicked position
   d. Infer correction direction using the same 1024x768 screenshot coordinate space:
      - If target is LEFT → decrease x
      - If target is RIGHT → increase x
      - If target is ABOVE → decrease y
      - If target is BELOW → increase y
   e. On next attempt, adjust coordinates accordingly and re-click.
11. Each correction must change the click position meaningfully. Do not repeat identical coordinates.

EFFICIENCY:
12. Batch independent tool actions into a single response when later actions do not require observing the result of earlier ones. This reduces round trips and speeds execution. Only the final tool call in a batch returns a screenshot; earlier calls return text-only confirmations.

Good candidates for batching:
- type_text, then key_press("return")
- repeated key presses for navigation (Tab, Shift-Tab, Arrow keys)
- click, then wait
- focus a field, then type_text
- scroll multiple increments
- open a menu, then wait for animation/loading
- escape to dismiss, then re-click a known target (retry/correction)

Do not batch actions when a later action depends on updated visual state, changed layout, new content, validation messages, popups, focus changes, or uncertain element positions.

Examples to avoid batching:
- click Search, then click a result that has not appeared yet
- submit a form, then click where a confirmation button should appear
- open a dropdown, then choose an option before seeing the menu
- close a modal, then click an underlying button without confirming the screen state
- click a tab, then interact with content that may load differently

13. For deterministic non-visual operations (file edits, system checks, data transforms, script execution), prefer `run_terminal_command` over GUI interaction. Keep GUI for tasks that depend on visual interpretation or UI state.

COMPLETION:
14. When the task is fully completed, call the `done` tool with a concise summary of what was achieved.

SCREEN-SHARE AWARENESS:
15. If the Context contains `user_visible_surface: ...`, the human user is
    currently watching that surface through screen-sharing. Your screenshots
    must match what they see — otherwise you will narrate things they
    cannot see and your guidance will be wrong.
16. On the first screenshot after that notice (or after any "[user-visible
    surface changed]" follow-up), briefly describe what you see and ask
    the user whether the view matches their screen.
17. If the user says it doesn't match, do not keep clicking blindly. Take
    another screenshot, describe it again, and keep iterating until you
    both agree on the visible surface.
18. When no `user_visible_surface` line is present, behave normally.
""" # noqa: E501


class ComputerUseExecutor(ExecutorProvider):
    """Execute GUI tasks via an agentic tool-calling loop.

    The model sees screenshots as inline images, and calls normal function tools
    (screenshot, left_click, type_text, key_press, etc.) to drive the GUI.
    This works through any API proxy that supports standard tool calling.

    Usage:
        executor = ComputerUseExecutor(api_key=..., model=..., platform=platform)
        await executor.start_task("Open Teams and create a meeting")
        async for event in executor.get_events():
            ...
        await executor.close()
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        platform: Platform,
        base_url: str | None = None,
        display_width: int = LLM_SCREENSHOT_WIDTH,
        display_height: int = LLM_SCREENSHOT_HEIGHT,
        max_iterations: int = _MAX_ITERATIONS,
        system_prompt: str = "",
        image_keep_last: int | None = None,
        enable_terminal: bool = True,
        mcp_terminal_command: list[str] | None = None,
    ) -> None:
        self._client, self._model, self._use_openai = create_sync_client(
            provider="anthropic" if model.startswith("claude") else "openai",
            api_key=api_key,
            model=model,
            base_url=base_url,
        )

        # Per-instance tool list — extends _BASE_TOOLS with optional MCP-backed tools.
        self._enable_terminal = enable_terminal
        self._mcp_terminal_command = (
            list(mcp_terminal_command) if mcp_terminal_command
            else list(_DEFAULT_MCP_TERMINAL_COMMAND)
        )
        self._tools: list[dict[str, Any]] = list(_BASE_TOOLS)
        if enable_terminal:
            self._tools.append(_TERMINAL_TOOL)
            self._tools.append(_SEND_INPUT_TOOL)
        self._tools_by_name: dict[str, dict[str, Any]] = {
            t["name"]: t for t in self._tools
        }

        if self._use_openai:
            self._openai_tools = [
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                }
                for t in self._tools
            ]
        else:
            # Tools with cache_control on the last definition so the
            # entire system+tools prefix is cached across iterations.
            self._cached_tools = [
                *self._tools[:-1],
                {**self._tools[-1], "cache_control": {"type": "ephemeral"}},
            ]
        self._platform = platform
        self._display_width = display_width
        self._display_height = display_height
        self._max_iterations = max_iterations
        base_prompt = system_prompt or _SYSTEM_PROMPT
        if enable_terminal:
            base_prompt = base_prompt + _TERMINAL_PROMPT_ADDENDUM
        self._system_prompt = base_prompt

        self._event_queue: asyncio.Queue[ExecutorEvent] = asyncio.Queue()
        self._loop_task: asyncio.Task | None = None
        self._coords = CoordinateMapper(platform, display_width, display_height)
        self._cancelled = False
        self._image_keep_last = image_keep_last
        self._messages: list[dict[str, Any]] = []

        # Lazy-spawned terminal MCP client. Created on first use so a missing
        # `npx` (or other launch failure) surfaces as a tool error to the model
        # rather than as a constructor exception.
        self._terminal: _TerminalMCP | None = None
        self._terminal_lock = asyncio.Lock()

    # ── ExecutorProvider interface ────────────────────────

    async def start_task(
        self,
        instruction: str,
        context: str | ExecutorContext = "",
        content_blocks: list[str | tuple[bytes, str]] | None = None,
    ) -> None:
        prompt = instruction
        context_str = str(context) if context else ""
        if context_str:
            prompt = f"Context: {context_str}\n\nTask: {instruction}"

        # Lock onto the active display so the first coord-taking tool call
        # (which may run before the model's first screenshot) maps to the
        # right monitor. _take_screenshot refreshes this on every capture.
        self._coords.refresh()

        self._cancelled = False
        self._content_blocks = content_blocks
        self._loop_task = asyncio.create_task(self._agentic_loop(prompt))

    async def send_message(self, message: str) -> None:
        """Inject a follow-up message and resume the agentic loop.

        Appends to existing conversation history so the model sees
        what it did before and can correct based on context.
        """
        self._messages.append({
            "role": "user",
            "content": [{"type": "text", "text": message}],
        })
        self._cancelled = False
        self._event_queue = asyncio.Queue()
        self._loop_task = asyncio.create_task(
            self._agentic_loop_resume()
        )

    async def get_events(self) -> AsyncIterator[ExecutorEvent]:
        while True:
            event = await self._event_queue.get()
            yield event
            if event.type in (ExecutorEventType.DONE, ExecutorEventType.ERROR):
                break

    async def interrupt(self) -> None:
        self._cancelled = True
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._event_queue.put_nowait(
            ExecutorEvent(type=ExecutorEventType.DONE, message="Interrupted")
        )

    async def close(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._terminal is not None:
            try:
                await self._terminal.close()
            except Exception as e:
                log.warning("Error closing terminal MCP: %s", e)
            self._terminal = None

    async def _ensure_terminal(self) -> _TerminalMCP:
        """Lazily spawn the terminal MCP server and return the client."""
        async with self._terminal_lock:
            if self._terminal is None:
                term = _TerminalMCP(self._mcp_terminal_command)
                await term.start()
                self._terminal = term
                log.info(
                    "Spawned MCP terminal server: %s",
                    " ".join(self._mcp_terminal_command),
                )
            return self._terminal

    def _drain_terminal_notifications(
        self, messages: list[dict[str, Any]],
    ) -> None:
        """Drain pending terminal notifications into the message history.

        Appends a user message with all pending notifications so the model
        sees them on its next turn. No-op if no notifications are pending
        or terminal is not active.
        """
        if self._terminal is None:
            return
        pending = self._terminal.notifications
        if not pending:
            return
        text = "\n\n".join(pending)
        count = len(pending)
        pending.clear()
        log.info("Injecting %d terminal notification(s)", count)
        text_type = "input_text" if self._use_openai else "text"
        messages.append({
            "role": "user",
            "content": [{"type": text_type, "text": text}],
        })

    # ── Core agentic loop ────────────────────────────────

    async def _agentic_loop(self, instruction: str) -> None:
        if self._use_openai:
            return await self._agentic_loop_openai(instruction)
        return await self._agentic_loop_anthropic(instruction)

    async def _agentic_loop_anthropic(self, instruction: str) -> None:
        try:
            user_content: list[dict[str, Any]] = []
            if self._content_blocks:
                for block in self._content_blocks:
                    if isinstance(block, str):
                        user_content.append({"type": "text", "text": block})
                    else:
                        img_data, media_type = block
                        b64 = base64.b64encode(img_data).decode("ascii")
                        user_content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        })
            user_content.append({"type": "text", "text": instruction})
            self._messages = [
                {"role": "user", "content": user_content},
            ]

            await self._run_anthropic_loop()

        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("Executor error: %s", e, exc_info=True)
            self._event_queue.put_nowait(ExecutorEvent(
                type=ExecutorEventType.ERROR,
                error=str(e),
            ))

    async def _agentic_loop_resume(self) -> None:
        """Resume the agentic loop with existing message history."""
        try:
            if self._use_openai:
                await self._run_openai_loop()
            else:
                await self._run_anthropic_loop()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("Executor error: %s", e, exc_info=True)
            self._event_queue.put_nowait(ExecutorEvent(
                type=ExecutorEventType.ERROR,
                error=str(e),
            ))

    async def _run_anthropic_loop(self) -> None:
        """Core Anthropic agentic loop operating on self._messages."""
        messages = self._messages

        last_text = ""
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation = 0
        total_cache_read = 0

        for iteration in range(self._max_iterations):
            if self._cancelled:
                break

            log.info("Iteration %d/%d", iteration + 1, self._max_iterations)
            self._event_queue.put_nowait(ExecutorEvent(
                type=ExecutorEventType.ITERATION,
                message=str(iteration + 1),
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            ))

            # Inject pending terminal notifications before the API call.
            self._drain_terminal_notifications(messages)

            # Call the API (system + tools marked for prompt caching)
            trimmed_messages = self._trim_request_messages(messages)
            try:
                response = await asyncio.to_thread(
                    self._client.messages.create,
                    model=self._model,
                    max_tokens=DEFAULT_MAX_TOKENS,
                    tools=self._cached_tools,
                    messages=trimmed_messages,
                    system=[
                        {
                            "type": "text",
                            "text": self._system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                )
            except Exception:
                self._dump_payload(trimmed_messages)
                raise

            # Accumulate token usage
            if response.usage:
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                total_cache_creation += response.usage.cache_creation_input_tokens or 0
                total_cache_read += response.usage.cache_read_input_tokens or 0

            log.info(
                "Response: stop=%s blocks=%s tok=%d+%d"
                " cc=%d cr=%d (total: %d+%d cc=%d cr=%d)",
                response.stop_reason,
                [b.type for b in response.content],
                response.usage.input_tokens if response.usage else 0,
                response.usage.output_tokens if response.usage else 0,
                response.usage.cache_creation_input_tokens or 0 if response.usage else 0,
                response.usage.cache_read_input_tokens or 0 if response.usage else 0,
                total_input_tokens,
                total_output_tokens,
                total_cache_creation,
                total_cache_read,
            )

            # Process response content blocks
            tool_results: list[dict[str, Any]] = []
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            num_tool_calls = len(tool_use_blocks)
            tool_call_index = 0

            for block in response.content:
                if self._cancelled:
                    break

                if block.type == "text":
                    last_text = block.text
                    self._event_queue.put_nowait(ExecutorEvent(
                        type=ExecutorEventType.MESSAGE,
                        message=block.text,
                    ))

                elif block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    tool_call_index += 1
                    is_last_tool = tool_call_index == num_tool_calls

                    log.info(
                        "Tool call: %s(%s) [%d/%d]",
                        tool_name, tool_input,
                        tool_call_index, num_tool_calls,
                    )

                    self._event_queue.put_nowait(ExecutorEvent(
                        type=ExecutorEventType.TOOL_CALL,
                        tool_name=tool_name,
                        tool_args=tool_input,
                    ))

                    # Handle "done" tool — task complete
                    if tool_name == "done":
                        summary = tool_input.get("summary", "Task completed")
                        token_summary = self._format_token_summary(
                            total_input_tokens, total_output_tokens,
                            total_cache_creation, total_cache_read,
                        )
                        log.info("Task done. %s", token_summary)
                        # Append text-only assistant message so
                        # send_message can resume with clean history.
                        messages.append({
                            "role": "assistant",
                            "content": [{"type": "text", "text": summary}],
                        })
                        self._event_queue.put_nowait(ExecutorEvent(
                            type=ExecutorEventType.DONE,
                            message=f"{summary}\n{token_summary}",
                        ))
                        return

                    # Execute the tool and get result.
                    # Only include screenshot on the last tool call in a batch
                    # to avoid wasting tokens on intermediate screenshots.
                    result = await self._execute_tool(
                        tool_name, tool_input,
                        include_screenshot=is_last_tool,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            # Append assistant response to message history
            # Only keep fields the API expects — model_dump() may include
            # extra SDK fields (e.g. 'caller') that cause 400 errors.
            messages.append({
                "role": "assistant",
                "content": [self._serialize_block(b) for b in response.content],
            })

            # If no tool calls, model is done
            if not tool_results or response.stop_reason == "end_turn":
                token_summary = self._format_token_summary(
                    total_input_tokens, total_output_tokens,
                    total_cache_creation, total_cache_read,
                )
                log.info("Task ended (end_turn). %s", token_summary)
                self._event_queue.put_nowait(ExecutorEvent(
                    type=ExecutorEventType.DONE,
                    message=f"{last_text or 'Task completed'}\n{token_summary}",
                ))
                return

            # Append tool results and continue
            messages.append({"role": "user", "content": tool_results})

        # Max iterations reached
        token_summary = self._format_token_summary(
            total_input_tokens, total_output_tokens,
            total_cache_creation, total_cache_read,
        )
        log.info("Max iterations reached. %s", token_summary)
        self._event_queue.put_nowait(ExecutorEvent(
            type=ExecutorEventType.DONE,
            message=(
                f"Reached max iterations ({self._max_iterations})."
                f" Last: {last_text}\n{token_summary}"
            ),
        ))

    async def _agentic_loop_openai(self, instruction: str) -> None:
        try:
            user_content: list[dict[str, Any]] = []
            if self._content_blocks:
                for block in self._content_blocks:
                    if isinstance(block, str):
                        user_content.append({"type": "input_text", "text": block})
                    else:
                        img_data, media_type = block
                        b64 = base64.b64encode(img_data).decode("ascii")
                        user_content.append({
                            "type": "input_image",
                            "image_url": f"data:{media_type};base64,{b64}",
                            "detail": "auto",
                        })
            user_content.append({"type": "input_text", "text": instruction})
            self._messages = [
                {"role": "user", "content": user_content},
            ]

            await self._run_openai_loop()

        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("Executor error: %s", e, exc_info=True)
            self._event_queue.put_nowait(ExecutorEvent(
                type=ExecutorEventType.ERROR,
                error=str(e),
            ))

    async def _run_openai_loop(self) -> None:
        """Core OpenAI agentic loop operating on self._messages (Responses API)."""
        messages = self._messages

        last_text = ""
        total_input_tokens = 0
        total_output_tokens = 0

        for iteration in range(self._max_iterations):
            if self._cancelled:
                break

            log.info("Iteration %d/%d", iteration + 1, self._max_iterations)
            self._event_queue.put_nowait(ExecutorEvent(
                type=ExecutorEventType.ITERATION,
                message=str(iteration + 1),
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            ))

            # Inject pending terminal notifications before the API call.
            self._drain_terminal_notifications(messages)

            trimmed_messages = self._trim_request_messages(messages)
            try:
                response = await asyncio.to_thread(
                    self._client.responses.create,
                    model=self._model,
                    max_output_tokens=DEFAULT_MAX_TOKENS,
                    tools=self._openai_tools,
                    input=trimmed_messages,
                    instructions=self._system_prompt,
                )
            except Exception:
                self._dump_payload(trimmed_messages)
                raise

            # Parse output items
            output_items = response.output or []

            if response.usage:
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens

            # Extract text, reasoning, and function calls from output
            text_content = ""
            reasoning = ""
            function_calls: list[Any] = []
            for item in output_items:
                item_type = getattr(item, "type", "")
                if item_type == "reasoning":
                    for part in getattr(item, "summary", []) or []:
                        if getattr(part, "type", "") == "summary_text":
                            reasoning += getattr(part, "text", "")
                elif item_type == "message":
                    for part in getattr(item, "content", []):
                        if getattr(part, "type", "") == "output_text":
                            text_content += part.text
                elif item_type == "function_call":
                    function_calls.append(item)

            log.info(
                "Response: status=%s function_calls=%d tok=%d+%d (total: %d+%d)",
                response.status,
                len(function_calls),
                response.usage.input_tokens if response.usage else 0,
                response.usage.output_tokens if response.usage else 0,
                total_input_tokens,
                total_output_tokens,
            )

            if text_content or reasoning:
                last_text = text_content or ""
                self._event_queue.put_nowait(ExecutorEvent(
                    type=ExecutorEventType.MESSAGE,
                    message=text_content or "",
                    reasoning=reasoning,
                ))

            # Process function calls
            tool_result_items: list[dict[str, Any]] = []
            screenshot_b64: str | None = None

            for i, fc in enumerate(function_calls):
                tool_name = fc.name
                tool_input = json.loads(fc.arguments)
                is_last_tool = i == len(function_calls) - 1

                log.info(
                    "Tool call: %s(%s) [%d/%d]",
                    tool_name, tool_input, i + 1, len(function_calls),
                )

                self._event_queue.put_nowait(ExecutorEvent(
                    type=ExecutorEventType.TOOL_CALL,
                    tool_name=tool_name,
                    tool_args=tool_input,
                ))

                if tool_name == "done":
                    summary = tool_input.get("summary", "Task completed")
                    token_summary = self._format_token_summary(
                        total_input_tokens, total_output_tokens,
                    )
                    log.info("Task done. %s", token_summary)
                    messages.append({
                        "role": "assistant",
                        "content": summary,
                    })
                    self._event_queue.put_nowait(ExecutorEvent(
                        type=ExecutorEventType.DONE,
                        message=f"{summary}\n{token_summary}",
                    ))
                    return

                result = await self._execute_tool(
                    tool_name, tool_input,
                    include_screenshot=is_last_tool,
                )

                text_parts = [r["text"] for r in result if r.get("type") == "text"]
                if is_last_tool:
                    for r in result:
                        if r.get("type") == "image":
                            screenshot_b64 = r["source"]["data"]

                tool_result_items.append({
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": "\n".join(text_parts) or "OK",
                })

            # Append response output items to history for next turn
            for item in output_items:
                messages.append(item)

            if not function_calls:
                token_summary = self._format_token_summary(
                    total_input_tokens, total_output_tokens,
                )
                log.info("Task ended (stop). %s", token_summary)
                self._event_queue.put_nowait(ExecutorEvent(
                    type=ExecutorEventType.DONE,
                    message=f"{last_text or 'Task completed'}\n{token_summary}",
                ))
                return

            # Append tool results
            messages.extend(tool_result_items)

            # Append screenshot as user message so model can see the screen
            if screenshot_b64:
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{screenshot_b64}",
                            "detail": "auto",
                        },
                    ],
                })

        token_summary = self._format_token_summary(
            total_input_tokens, total_output_tokens,
        )
        log.info("Max iterations reached. %s", token_summary)
        self._event_queue.put_nowait(ExecutorEvent(
            type=ExecutorEventType.DONE,
            message=(
                f"Reached max iterations ({self._max_iterations})."
                f" Last: {last_text}\n{token_summary}"
            ),
        ))

    # ── Message trimming ────────────────────────────────

    @staticmethod
    def _truncate_reasoning(reasoning: str) -> str:
        """Use truncated reasoning as content placeholder when content is empty."""
        if not reasoning:
            return ""
        if len(reasoning) <= _REASONING_TRUNCATE:
            return reasoning
        return reasoning[:_REASONING_TRUNCATE] + " [truncated]"

    def _trim_request_messages(
        self, messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self._image_keep_last is not None:
            return self._trim_old_images(messages)
        return messages

    def _trim_old_images(
        self, messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return messages with images stripped from older entries.

        Preserves images in the first message (initial user with skill
        reference images) and in the last *image_keep_last* messages.
        Handles OpenAI Responses API (input_image), Chat Completions
        (image_url), and Anthropic (image) formats, including images
        nested inside tool_result blocks.
        """
        keep = self._image_keep_last
        if keep is None:
            return messages
        if len(messages) <= keep:
            return messages

        cutoff = len(messages) - keep
        trimmed: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            # Always keep initial user message (0) intact
            if i < 1 or i >= cutoff:
                trimmed.append(msg)
                continue

            # SDK response objects (non-dict) pass through untouched
            if not isinstance(msg, dict):
                trimmed.append(msg)
                continue

            content = msg.get("content")
            if isinstance(content, list):
                filtered = []
                for b in content:
                    if not isinstance(b, dict):
                        filtered.append(b)
                        continue
                    btype = b.get("type")
                    # Strip top-level image blocks
                    if btype in ("image_url", "image", "input_image"):
                        continue
                    # Strip images nested inside tool_result blocks
                    if btype == "tool_result":
                        b = self._strip_tool_result_images(b)
                    filtered.append(b)
                if not filtered:
                    text_type = "input_text" if self._use_openai else "text"
                    trimmed.append({**msg, "content": [{"type": text_type, "text": "[screenshot omitted]"}]})
                else:
                    trimmed.append({**msg, "content": filtered})
            else:
                trimmed.append(msg)
        return trimmed

    @staticmethod
    def _strip_tool_result_images(block: dict[str, Any]) -> dict[str, Any]:
        """Replace tool_result content with '[screenshot omitted]' when images are present.

        Replaces ALL content (not just images) to avoid leaving misleading
        text like 'Result screenshot below.' that references missing images.
        """
        content = block.get("content")
        if not isinstance(content, list):
            return block
        has_image = any(
            isinstance(b, dict) and b.get("type") in ("image_url", "image", "input_image")
            for b in content
        )
        if not has_image:
            return block
        return {**block, "content": [{"type": "text", "text": "[screenshot omitted]"}]}

    # ── Payload dump ─────────────────────────────────────

    def _dump_payload(self, messages: list[dict[str, Any]]) -> None:
        """Dump the API payload to _cua_last_payload.json on error for debugging."""
        def _redact(obj: Any) -> Any:
            if isinstance(obj, dict):
                # Anthropic image block
                if obj.get("type") == "image" and isinstance(obj.get("source"), dict):
                    src = obj["source"]
                    if "data" in src:
                        return {**obj, "source": {**src, "data": f"<base64 {len(src['data'])} chars ~{len(src['data'])*3//4} bytes>"}}
                # OpenAI Chat Completions image_url block
                if obj.get("type") == "image_url" and isinstance(obj.get("image_url"), dict):
                    url = obj["image_url"].get("url", "")
                    if url.startswith("data:"):
                        return {**obj, "image_url": {"url": f"<data_url {len(url)} chars>"}}
                # OpenAI Responses API input_image block
                if obj.get("type") == "input_image" and isinstance(obj.get("image_url"), str):
                    url = obj["image_url"]
                    if url.startswith("data:"):
                        return {**obj, "image_url": f"<data_url {len(url)} chars>"}
                return {k: _redact(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_redact(item) for item in obj]
            return obj

        token_key = "max_output_tokens" if self._use_openai else "max_tokens"
        payload = {
            "model": self._model,
            token_key: DEFAULT_MAX_TOKENS,
            "messages": _redact(messages),
            "message_count": len(messages),
        }
        try:
            with open("_cua_last_payload.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            log.info("Payload dumped to _cua_last_payload.json (%d messages)",
                     len(messages))
        except Exception as e:
            log.warning("Failed to dump payload: %s", e)

    # ── Tool execution ──────────────────────────────────

    @staticmethod
    def _format_token_summary(
        input_tokens: int, output_tokens: int,
        cache_creation: int = 0, cache_read: int = 0,
    ) -> str:
        total = input_tokens + output_tokens
        parts = [f"{input_tokens:,} input + {output_tokens:,} output = {total:,} total"]
        if cache_creation or cache_read:
            parts.append(f"cache: {cache_creation:,} created, {cache_read:,} read")
        return "Token usage: " + " | ".join(parts)

    def _safe_int(self, value: Any) -> int:
        """Convert value to int, handling strings like '512, 384'."""
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(round(value))
        s = str(value).strip()
        # Handle malformed values like "955, 332" — take first number
        if "," in s:
            s = s.split(",")[0].strip()
        return int(s)

    @staticmethod
    def _format_tool_input(input_data: dict[str, Any]) -> str:
        return json.dumps(input_data, ensure_ascii=True, sort_keys=True)

    def _tool_schema_text(self, name: str) -> str:
        tool = self._tools_by_name.get(name)
        if not tool:
            return ""
        return json.dumps(tool["input_schema"], ensure_ascii=True, sort_keys=True)

    @staticmethod
    def _tool_error_detail(error: Exception) -> str:
        if isinstance(error, KeyError) and error.args:
            return f"missing required field {error.args[0]!r}"
        return str(error)

    def _tool_error_hint(self, name: str, input_data: dict[str, Any]) -> str:
        if name in {"left_click", "right_click", "double_click", "mouse_move", "scroll"}:
            x_value = input_data.get("x")
            if "y" not in input_data and isinstance(x_value, str) and "," in x_value:
                return (
                    "Pass x and y as separate integer fields, "
                    "not as a single comma-separated string, "
                    'for example {"x": 100, "y": 200}'
                )
            return "Pass integer x and y fields that match the tool schema"
        return "Match the tool input to the schema exactly"

    def _format_tool_error(self, name: str, input_data: dict[str, Any], error: Exception) -> str:
        parts = [
            f"Error executing {name}: {self._tool_error_detail(error)}.",
            f"Received input: {self._format_tool_input(input_data)}.",
        ]
        schema_text = self._tool_schema_text(name)
        if schema_text:
            parts.append(f"Expected schema: {schema_text}.")
            parts.append(f"Hint: {self._tool_error_hint(name, input_data)}.")
        return " ".join(parts)

    async def _screenshot_result(self) -> dict[str, Any]:
        """Take a screenshot and return it as an image content block."""
        screenshot_b64 = await self._take_screenshot()
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": screenshot_b64,
            },
        }

    def _detail_crop(self, cx: int, cy: int) -> dict[str, Any] | None:
        """Crop a 2x-zoomed detail view around (cx, cy) with coordinate grid.

        Uses self._last_screenshot_img (set by _take_screenshot).
        Returns an image content block, or None if no screenshot is cached.

        The crop is a square of side `max(width, height) / 4` centered
        on the click — ~25% of the screen's longest dimension. Grid
        step is the module-level constant `_DETAIL_GRID_STEP`.
        """
        from PIL import ImageDraw, ImageFont

        img = getattr(self, "_last_screenshot_img", None)
        if img is None:
            return None

        crop_r = max(1, max(self._display_width, self._display_height) // 8)
        step = _DETAIL_GRID_STEP
        W, H = img.size
        x1, y1 = max(0, cx - crop_r), max(0, cy - crop_r)
        x2, y2 = min(W, cx + crop_r), min(H, cy + crop_r)
        crop = img.crop((x1, y1, x2, y2))

        cw, ch = crop.size
        crop = crop.resize((cw * 2, ch * 2), Image.LANCZOS)  # type: ignore[attr-defined]
        draw = ImageDraw.Draw(crop)

        font = ImageFont.load_default()

        for gx in range((x1 // step) * step, x2 + 1, step):
            sx = (gx - x1) * 2
            if 0 <= sx <= cw * 2:
                draw.line([(sx, 0), (sx, ch * 2)], fill="red", width=1)
                txt = str(gx)
                bbox = font.getbbox(txt)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                draw.rectangle([(sx + 1, 0), (sx + 1 + tw + 4, th + 4)], fill="white")
                draw.text((sx + 3, 1), txt, fill="black", font=font)

        for gy in range((y1 // step) * step, y2 + 1, step):
            sy = (gy - y1) * 2
            if 0 <= sy <= ch * 2:
                draw.line([(0, sy), (cw * 2, sy)], fill="red", width=1)
                txt = str(gy)
                bbox = font.getbbox(txt)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                draw.rectangle([(0, sy + 1), (tw + 4, sy + 1 + th + 4)], fill="white")
                draw.text((2, sy + 2), txt, fill="black", font=font)

        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=LLM_JPEG_QUALITY, optimize=True)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        }

    def _coord_result(
        self, action: str, ix: int, iy: int, screenshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Build tool_result blocks for a coordinate-based action.

        Returns: [text, full_screenshot, detail_caption, detail_image].
        """
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": f"{action}. Result screenshot below."},
            screenshot,
        ]
        detail = self._detail_crop(ix, iy)
        if detail is not None:
            W, H = self._display_width, self._display_height
            crop_r = max(1, max(self._display_width, self._display_height) // 8)
            x1, y1 = max(0, ix - crop_r), max(0, iy - crop_r)
            x2, y2 = min(W, ix + crop_r), min(H, iy + crop_r)
            blocks.append({
                "type": "text",
                "text": (
                    f"[Detail view around ({ix}, {iy})"
                    f" — region x={x1}..{x2}, y={y1}..{y2},"
                    f" with coordinate grid overlay]"
                ),
            })
            blocks.append(detail)
        return blocks

    async def _execute_tool(
        self, name: str, input_data: dict[str, Any],
        *, include_screenshot: bool = True,
    ) -> list[dict[str, Any]]:
        """Execute a tool call and return content blocks for the tool_result.

        Every action automatically includes a post-action screenshot so the
        model doesn't need to call screenshot separately after each action.
        """
        try:
            if name == "screenshot":
                return [await self._screenshot_result()]

            elif name == "left_click":
                ix, iy = self._safe_int(input_data["x"]), self._safe_int(input_data["y"])
                x, y = self._coords.to_actual(ix, iy)
                self._platform.click(x, y)
                await asyncio.sleep(0.5)
                if not include_screenshot:
                    return [{"type": "text", "text": f"Clicked at ({ix}, {iy})"}]
                ss = await self._screenshot_result()
                return self._coord_result(f"Clicked at ({ix}, {iy})", ix, iy, ss)

            elif name == "right_click":
                ix, iy = self._safe_int(input_data["x"]), self._safe_int(input_data["y"])
                x, y = self._coords.to_actual(ix, iy)
                self._platform.click(x, y, button="right")
                await asyncio.sleep(0.5)
                if not include_screenshot:
                    return [{"type": "text", "text": f"Right-clicked at ({ix}, {iy})"}]
                ss = await self._screenshot_result()
                return self._coord_result(f"Right-clicked at ({ix}, {iy})", ix, iy, ss)

            elif name == "double_click":
                ix, iy = self._safe_int(input_data["x"]), self._safe_int(input_data["y"])
                x, y = self._coords.to_actual(ix, iy)
                self._platform.double_click(x, y)
                await asyncio.sleep(0.5)
                if not include_screenshot:
                    return [{"type": "text", "text": f"Double-clicked at ({ix}, {iy})"}]
                ss = await self._screenshot_result()
                return self._coord_result(f"Double-clicked at ({ix}, {iy})", ix, iy, ss)

            elif name == "mouse_move":
                ix, iy = self._safe_int(input_data["x"]), self._safe_int(input_data["y"])
                x, y = self._coords.to_actual(ix, iy)
                self._platform.move_cursor(x, y)
                await asyncio.sleep(0.2)
                if not include_screenshot:
                    return [{"type": "text", "text": f"Moved cursor to ({ix}, {iy})"}]
                ss = await self._screenshot_result()
                return self._coord_result(f"Moved cursor to ({ix}, {iy})", ix, iy, ss)

            elif name == "type_text":
                text = input_data["text"]
                self._platform.type_text(text)
                await asyncio.sleep(0.5)
                if not include_screenshot:
                    return [{"type": "text", "text": f"Typed: {text!r}"}]
                return [
                    {"type": "text", "text": f"Typed: {text!r}"},
                    await self._screenshot_result(),
                ]

            elif name == "key_press":
                keys_str = input_data["keys"]
                keys = parse_key_combo(keys_str)
                self._platform.key_press(*keys)
                await asyncio.sleep(0.5)
                if not include_screenshot:
                    return [{"type": "text", "text": f"Pressed: {keys_str}"}]
                return [
                    {"type": "text", "text": f"Pressed: {keys_str}"},
                    await self._screenshot_result(),
                ]

            elif name == "scroll":
                ix, iy = self._safe_int(input_data["x"]), self._safe_int(input_data["y"])
                x, y = self._coords.to_actual(ix, iy)
                direction = input_data["direction"]
                amount = input_data.get("amount", 3)
                self._platform.scroll(x, y, direction, amount)
                await asyncio.sleep(0.3)
                if not include_screenshot:
                    return [{
                        "type": "text",
                        "text": f"Scrolled {direction} {amount} steps at ({ix}, {iy})"
                    }]
                ss = await self._screenshot_result()
                return self._coord_result(
                    f"Scrolled {direction} {amount} steps at ({ix}, {iy})",
                    ix, iy, ss,
                )

            elif name == "wait":
                seconds = input_data.get("seconds", 2)
                await asyncio.sleep(seconds)
                if not include_screenshot:
                    return [{"type": "text", "text": f"Waited {seconds}s"}]
                return [
                    {"type": "text", "text": f"Waited {seconds}s"},
                    await self._screenshot_result(),
                ]

            elif name == "run_terminal_command":
                if not self._enable_terminal:
                    return [{"type": "text", "text": (
                        "run_terminal_command is not enabled for this executor."
                    )}]
                cmd = input_data["command"]
                timeout = float(input_data.get("timeout_seconds", 30))
                shell = input_data.get("shell")
                terminal = await self._ensure_terminal()
                output = await terminal.start_command(cmd, timeout, shell)
                if not include_screenshot:
                    return [{"type": "text", "text": output}]
                await asyncio.sleep(0.5)
                return [
                    {"type": "text", "text": output},
                    await self._screenshot_result(),
                ]

            elif name == "send_terminal_input":
                if not self._enable_terminal:
                    return [{"type": "text", "text": (
                        "send_terminal_input is not enabled for this executor."
                    )}]
                pid = int(input_data["pid"])
                text = input_data["input"]
                terminal = await self._ensure_terminal()
                output = await terminal.send_input(pid, text)
                if not include_screenshot:
                    return [{"type": "text", "text": output}]
                await asyncio.sleep(0.5)
                return [
                    {"type": "text", "text": output},
                    await self._screenshot_result(),
                ]

            else:
                return [{"type": "text", "text": f"Unknown tool: {name}"}]

        except Exception as e:
            log.error("Tool %s failed: %s", name, e, exc_info=True)
            return [{"type": "text", "text": self._format_tool_error(name, input_data, e)}]

    # ── Helpers ──────────────────────────────────────────

    async def _take_screenshot(self) -> str:
        """Capture screen, resize, encode as JPEG base64.

        Also stores the resized PIL Image in self._last_screenshot_img
        so _detail_crop can produce a zoomed detail view without
        re-capturing.
        """
        import tempfile
        from pathlib import Path

        from protean.platform.base import prepare_screenshot_for_llm

        tmp = Path(tempfile.gettempdir()) / f"protean_cu_screenshot_{time.monotonic_ns()}.png"

        # Re-detect the active display each time so moving windows between
        # monitors mid-task is handled.
        self._coords.refresh()
        display_index = self._coords.display_index

        self._platform.capture_display(display_index, tmp)
        raw_bytes = tmp.read_bytes()
        tmp.unlink(missing_ok=True)

        # Resize to exact API coordinate space + compress
        jpeg_bytes, _ = prepare_screenshot_for_llm(
            raw_bytes,
            max_width=self._display_width,
            max_height=self._display_height,
            quality=LLM_JPEG_QUALITY,
            exact_size=True,
        )

        # Keep resized PIL Image for detail crop
        self._last_screenshot_img = Image.open(io.BytesIO(jpeg_bytes)).copy()

        b64 = base64.standard_b64encode(jpeg_bytes).decode()
        log.debug("Screenshot: %d bytes base64", len(b64))
        return b64

    @staticmethod
    def _serialize_block(block: Any) -> dict[str, Any]:
        """Serialize a content block with only API-expected fields.

        The SDK's model_dump() may include extra fields (e.g. 'caller')
        that the API rejects when sent back in messages.
        """
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        return block.model_dump()


# ── Terminal MCP client ─────────────────────────────────────


def _truncate_terminal_output(text: str, max_bytes: int) -> str:
    """Truncate text to ~max_bytes, preserving head and tail with a marker."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    half = max_bytes // 2 - 64
    head = encoded[:half].decode("utf-8", errors="replace")
    tail = encoded[-half:].decode("utf-8", errors="replace")
    omitted = len(encoded) - 2 * half
    return (
        f"{head}\n\n"
        f"[... {omitted} bytes truncated — pipe through tail/head "
        f"or redirect to file to see full output ...]\n\n"
        f"{tail}"
    )


def _flatten_mcp_text(call_result: Any) -> str:
    """Concatenate text content from an MCP CallToolResult."""
    parts: list[str] = []
    content = getattr(call_result, "content", None) or []
    for c in content:
        text = getattr(c, "text", None)
        if text:
            parts.append(text)
        elif isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "\n".join(parts)


# Regex to strip DesktopCommander metadata lines from output.
# Keeps: ✅ Process completed (has exit code + runtime), ⏱️ timeout info.
# Strips everything else DC adds as framing.
_DC_METADATA_RE = re.compile(
    r"^\[Reading \d+ .*lines.*\]$"
    r"|^\(No output in requested range\)$"
    r"|^Process started with PID \d+.*$"
    r"|^Initial output:$"
    r"|^⏳ .*$"
    r"|^🔄 .*$"
    r"|^📤 Output:$"
    r"|^📭 \(No output produced\)$",
    re.MULTILINE,
)


def _strip_dc_metadata(text: str) -> str:
    """Remove DesktopCommander status/framing lines from output text."""
    cleaned = _DC_METADATA_RE.sub("", text)
    # Collapse runs of blank lines left by removal.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _clean_env() -> dict[str, str]:
    """Return a copy of the current environment with virtualenv variables removed.

    Prevents the Protean venv (activated by ``uv run``) from leaking into
    commands executed via ``run_terminal_command``, which should behave as if
    run in a normal user terminal.
    """
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        paths = env.get("PATH", "").split(os.pathsep)
        paths = [p for p in paths if not p.startswith(venv)]
        env["PATH"] = os.pathsep.join(paths)
    return env


class _TerminalMCP:
    """Lifecycle wrapper around DesktopCommanderMCP via stdio.

    Spawns the MCP server as a subprocess, holds a single ``ClientSession``,
    and exposes ``start_command`` for non-blocking command execution. Each
    command gets a background monitor task that polls for output and
    generates notifications (exit, idle, interactive prompt, size limit).

    The session is run on a dedicated background task so that the
    ``AsyncExitStack`` enter/exit always happen in the same task — required by
    ``mcp.client.stdio.stdio_client`` and ``ClientSession`` which are built on
    ``anyio`` task scopes.
    """

    def __init__(self, command: list[str]) -> None:
        if not command:
            raise ValueError("Terminal MCP command must not be empty")
        self._command = command
        self._session: Any = None
        self._stack: AsyncExitStack | None = None
        self._ready_evt = asyncio.Event()
        self._stop_evt = asyncio.Event()
        self._runner_task: asyncio.Task | None = None
        self._start_error: BaseException | None = None
        # Background monitor tasks keyed by PID.
        self._monitors: dict[int, asyncio.Task] = {}
        # Notification queue — drained by the agentic loop before each API call.
        self.notifications: list[str] = []

    async def start(self) -> None:
        self._runner_task = asyncio.create_task(self._run())
        # Wait for the session to be ready (or for startup to fail).
        done, _ = await asyncio.wait(
            {self._runner_task, asyncio.create_task(self._ready_evt.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._start_error is not None:
            raise RuntimeError(
                f"Failed to start terminal MCP ({' '.join(self._command)}): "
                f"{self._start_error}"
            ) from self._start_error
        if self._runner_task in done and not self._ready_evt.is_set():
            # Runner exited before ready — propagate any exception.
            exc = self._runner_task.exception()
            raise RuntimeError(
                f"Terminal MCP exited during startup: {exc}"
            ) from exc

    async def _run(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self._command[0],
            args=self._command[1:],
            env=_clean_env(),
        )
        try:
            async with AsyncExitStack() as stack:
                self._stack = stack
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._session = session
                self._ready_evt.set()
                # Block until close() is called.
                await self._stop_evt.wait()
        except BaseException as e:
            self._start_error = e
            self._ready_evt.set()
            raise
        finally:
            self._session = None
            self._stack = None

    async def close(self) -> None:
        # Cancel all monitor tasks.
        for task in list(self._monitors.values()):
            task.cancel()
        for task in list(self._monitors.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._monitors.clear()
        self._stop_evt.set()
        if self._runner_task is not None:
            try:
                await asyncio.wait_for(self._runner_task, timeout=5)
            except asyncio.TimeoutError:
                self._runner_task.cancel()
                try:
                    await self._runner_task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception:
                pass

    async def start_command(
        self, command: str, timeout_seconds: float, shell: str | None,
    ) -> str:
        """Start ``command`` asynchronously.

        Returns initial output (truncated). If the process is still running,
        a background monitor task is spawned to poll for output and generate
        notifications.
        """
        if self._session is None:
            return "Error: terminal MCP is not running."

        timeout_ms = max(500, int(timeout_seconds * 1000))
        start_args: dict[str, Any] = {
            "command": command,
            "timeout_ms": timeout_ms,
        }
        if shell:
            start_args["shell"] = shell

        read_timeout = timedelta(seconds=max(timeout_seconds + 5, 10))

        try:
            initial = await self._session.call_tool(
                "start_process", start_args, read_timeout_seconds=read_timeout,
            )
        except Exception as e:
            return f"Error launching command via terminal MCP: {e}"

        initial_text = _strip_dc_metadata(_flatten_mcp_text(initial))
        pid = _extract_pid(initial)

        # If start_process already returned the full output (process exited),
        # there's no PID to monitor — return immediately.
        if pid is None:
            return _truncate_terminal_output(
                initial_text or "[no output]",
                _TERMINAL_OUTPUT_MAX_BYTES,
            )

        # Check initial output for interactive prompt.
        if initial_text and _INTERACTIVE_PROMPT_RE.search(initial_text):
            self.notifications.append(
                f"[Terminal PID {pid}] Process is waiting for interactive "
                f"input. Last output:\n{_truncate_terminal_output(initial_text, 512)}\n"
                f"Use send_terminal_input(pid={pid}, input=\"...\") to respond."
            )

        # Process is still running — spawn background monitor.
        if _process_exited(initial):
            return _truncate_terminal_output(
                initial_text or "[no output]",
                _TERMINAL_OUTPUT_MAX_BYTES,
            )

        task = asyncio.create_task(
            self._monitor(pid, command, initial_text or ""),
        )
        self._monitors[pid] = task
        task.add_done_callback(lambda _t: self._monitors.pop(pid, None))

        result = f"[Process started: PID {pid}]\n"
        if initial_text:
            result += _truncate_terminal_output(
                initial_text, _TERMINAL_OUTPUT_MAX_BYTES,
            )
        else:
            result += "[no initial output — process running in background]"
        return result

    async def send_input(self, pid: int, text: str) -> str:
        """Send ``text`` to the stdin of process ``pid``.

        Uses DesktopCommanderMCP's ``interact_with_process`` tool.
        Returns the response text (truncated).
        """
        if self._session is None:
            return "Error: terminal MCP is not running."

        # Auto-append newline so the model sends "y" not "y\n".
        if not text.endswith("\n"):
            text += "\n"

        try:
            result = await self._session.call_tool(
                "interact_with_process",
                {"pid": pid, "input": text},
                read_timeout_seconds=timedelta(seconds=10),
            )
        except Exception as e:
            return f"Error sending input to PID {pid}: {e}"

        response = _strip_dc_metadata(_flatten_mcp_text(result))
        return _truncate_terminal_output(
            response or "[no response]",
            _TERMINAL_OUTPUT_MAX_BYTES,
        )

    async def _monitor(
        self, pid: int, command: str, initial_output: str,
    ) -> None:
        """Background monitor for a running process.

        Polls ``read_process_output`` every ~2s and generates notifications
        for: process exit, idle timeout, interactive prompt, output size limit.
        """
        output_chunks: list[str] = []
        if initial_output:
            output_chunks.append(initial_output)
        total_bytes = len(initial_output.encode("utf-8", errors="replace"))
        idle_count = 0
        initial_stripped = _strip_dc_metadata(initial_output)
        read_timeout = timedelta(seconds=10)

        try:
            while True:
                await asyncio.sleep(2)

                try:
                    read_result = await self._session.call_tool(
                        "read_process_output",
                        {"pid": pid, "timeout_ms": 2000},
                        read_timeout_seconds=read_timeout,
                    )
                except Exception as e:
                    log.warning("Monitor PID %d: read failed: %s", pid, e)
                    self.notifications.append(
                        f"[Terminal PID {pid}] Lost connection to process "
                        f"(command: {command}): {e}"
                    )
                    break

                chunk = _flatten_mcp_text(read_result)
                # For idle detection, check if there's real new output
                # (DC returns status text even when process produced nothing).
                stripped = _strip_dc_metadata(chunk)
                # DC's first read_process_output often re-returns the same
                # content that start_process already gave us. Skip duplicates.
                if stripped and initial_stripped and stripped in initial_stripped:
                    stripped = ""
                    initial_stripped = ""  # only skip once
                has_new_output = bool(stripped)

                if has_new_output:
                    log.debug(
                        "Monitor PID %d: got %d bytes: %r",
                        pid, len(stripped), stripped[:100],
                    )
                    output_chunks.append(stripped)
                    total_bytes += len(stripped.encode("utf-8", errors="replace"))
                    idle_count = 0

                    # Interactive prompt detection.
                    if _INTERACTIVE_PROMPT_RE.search(stripped):
                        self.notifications.append(
                            f"[Terminal PID {pid}] Process is waiting for "
                            f"interactive input (command: {command}). "
                            f"Last output:\n{_truncate_terminal_output(stripped, 512)}\n"
                            f"Use send_terminal_input(pid={pid}, input=\"...\") to respond."
                        )
                        # Don't break — process might still produce more output
                        # if the prompt times out on its own.

                    # Size watchdog.
                    if total_bytes > _TERMINAL_MAX_OUTPUT_BYTES:
                        log.warning(
                            "Monitor PID %d: output exceeded %d bytes, terminating",
                            pid, _TERMINAL_MAX_OUTPUT_BYTES,
                        )
                        try:
                            await self._session.call_tool(
                                "force_terminate", {"pid": pid},
                                read_timeout_seconds=timedelta(seconds=5),
                            )
                        except Exception:
                            pass
                        all_output = "\n".join(output_chunks)
                        self.notifications.append(
                            f"[Terminal PID {pid}] Process terminated: output "
                            f"exceeded {_TERMINAL_MAX_OUTPUT_BYTES // 1024 // 1024} MB "
                            f"(command: {command}).\n"
                            f"Output (truncated):\n"
                            f"{_truncate_terminal_output(all_output, _TERMINAL_OUTPUT_MAX_BYTES)}"
                        )
                        return

                else:
                    idle_count += 1
                    if idle_count >= _TERMINAL_IDLE_POLLS_LIMIT:
                        all_output = "\n".join(output_chunks)
                        self.notifications.append(
                            f"[Terminal PID {pid}] Process idle for "
                            f"~{idle_count * 2}s — may be hung or waiting "
                            f"for input (command: {command}).\n"
                            f"Output so far:\n"
                            f"{_truncate_terminal_output(all_output, _TERMINAL_OUTPUT_MAX_BYTES)}"
                        )
                        # Reset idle count so we don't spam notifications.
                        idle_count = 0

                if _process_exited(read_result):
                    all_output = "\n".join(output_chunks)
                    self.notifications.append(
                        f"[Terminal PID {pid}] Process finished "
                        f"(command: {command}).\n"
                        f"Output:\n"
                        f"{_truncate_terminal_output(all_output, _TERMINAL_OUTPUT_MAX_BYTES)}"
                    )
                    return

        except asyncio.CancelledError:
            return


def _extract_pid(call_result: Any) -> int | None:
    """Best-effort PID extraction from a DesktopCommander start_process result.

    The server's response format isn't strictly typed across versions: it may
    embed the PID in structured fields, in JSON inside a text block, or as a
    plain ``PID: 1234`` substring. We try them in that order.
    """
    structured = getattr(call_result, "structuredContent", None)
    if isinstance(structured, dict):
        for key in ("pid", "process_id", "processId"):
            v = structured.get(key)
            if isinstance(v, int):
                return v

    text = _flatten_mcp_text(call_result)
    if not text:
        return None

    # Try JSON payload
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            for key in ("pid", "process_id", "processId"):
                v = obj.get(key)
                if isinstance(v, int):
                    return v
    except (ValueError, TypeError):
        pass

    # Fallback: regex-style scan for "pid 1234" / "PID: 1234"
    m = re.search(r"\b[Pp][Ii][Dd]\s*[:=]?\s*(\d+)\b", text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _process_exited(call_result: Any) -> bool:
    """Heuristic: did read_process_output indicate the process is gone?"""
    structured = getattr(call_result, "structuredContent", None)
    if isinstance(structured, dict):
        for key in ("is_running", "isRunning", "running"):
            if key in structured:
                return not bool(structured[key])
        if structured.get("exit_code") is not None:
            return True
        if structured.get("exitCode") is not None:
            return True

    text = _flatten_mcp_text(call_result).lower()
    if not text:
        return False
    markers = (
        "process exited", "process completed", "process finished",
        "exit code", "process not found", "no such process",
        "process is no longer running",
    )
    return any(m in text for m in markers)

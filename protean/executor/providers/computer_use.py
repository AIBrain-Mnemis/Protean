"""Computer Use executor — drives GUI via tool-calling agentic loop.

The model sees screenshots as inline images and calls standard function tools
(screenshot, click, drag, type_text, key_press, scroll, etc.) to drive the GUI.
Each iteration: model sees screen → decides action → we execute → take screenshot → loop.

This avoids the Claude Code proxy limitation where computer_20250124 tool inputs
get stripped, by using normal function-call tools that the proxy handles correctly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import TYPE_CHECKING, Any, AsyncIterator

from protean.config import DEFAULT_MAX_TOKENS
from protean.executor import ExecutorContext, ExecutorEvent, ExecutorEventType, ExecutorProvider
from protean.executor.actions import (
    GUI_TOOL_NAMES,
    GUI_TOOL_SPECS,
    ActionExecutor,
    ActionResult,
    format_tool_error,
)
from protean.executor.providers.prompts import (
    COMPUTER_USE_SYSTEM_PROMPT,
    TERMINAL_PROMPT_ADDENDUM,
)
from protean.llm import create_sync_client
from protean.platform.base import (
    LLM_SCREENSHOT_HEIGHT,
    LLM_SCREENSHOT_WIDTH,
    CoordinateMapper,
    active_display,
)

if TYPE_CHECKING:
    from protean.platform.base import Platform

log = logging.getLogger(__name__)

_MAX_ITERATIONS = 500
_REASONING_TRUNCATE = 500


# ── Tool definitions (standard function tools) ──────────────
#
# GUI actions (screenshot, click, drag, type, etc.) come from
# ``protean.executor.actions.GUI_TOOL_SPECS`` so the Anthropic /
# OpenAI function-tool list, the MCP server tool list, and
# ``ActionExecutor.dispatch`` all stay in lockstep — adding a tool in
# one place propagates everywhere. Only computer_use-specific tools
# (``done``, optional terminal tools) live in this file.

_GUI_TOOLS: list[dict[str, Any]] = [spec.to_function_tool() for spec in GUI_TOOL_SPECS]

# ``done`` is loop control: signals the agentic loop to break with the
# given summary. Not exposed via MCP (external CLI agents have their
# own task-complete signals — Claude Code's ResultMessage, Codex's
# stop reason).
_DONE_TOOL: dict[str, Any] = {
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
}


# ── Terminal tools ──────────────────────────────────────────
#
# TODO: the terminal toolset is weirdly placed.
#   - The "MCP-in-MCP" smell is real: we spawn DesktopCommanderMCP as
#     a subprocess and proxy two tools (run_terminal_command,
#     send_terminal_input) into our own function-tool surface. But
#     these tools are computer_use-only — external CLI agents have
#     their own Bash tools, so they're NOT exposed via
#     ``protean.mcp.server``.
#   - DesktopCommander gives us PID tracking, idle detection,
#     interactive-prompt detection, output-size watchdog, and
#     force_terminate for free. Reimplementing on top of native
#     ``asyncio.create_subprocess_exec`` is one to two hundred lines
#     of subprocess plumbing.
#   - Two cleanup options when we revisit:
#       (a) Replace DesktopCommander with a native ActionExecutor
#           method (e.g. ``run_command()``) so terminal tools become
#           regular ActionResult-returning actions like the rest.
#       (b) Drop the proxy entirely and require the caller to set up
#           their own terminal MCP (matches how Bash works in
#           Claude Code today).
#   - Until then, keep these tools / the _TerminalMCP wrapper /
#     _TERMINAL_PROMPT_ADDENDUM here. They are not part of the shared
#     GUI tool surface.

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


class ComputerUseExecutor(ExecutorProvider):
    """Execute GUI tasks via an agentic tool-calling loop.

    The model sees screenshots as inline images, and calls normal function tools
    (screenshot, click, drag, type_text, key_press, etc.) to drive the GUI.
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

        # Per-instance tool list = shared GUI tools + loop-control
        # (done) + optional terminal proxies.
        self._enable_terminal = enable_terminal
        self._mcp_terminal_command = (
            list(mcp_terminal_command) if mcp_terminal_command
            else list(_DEFAULT_MCP_TERMINAL_COMMAND)
        )
        self._tools: list[dict[str, Any]] = [*_GUI_TOOLS, _DONE_TOOL]
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
        base_prompt = system_prompt or COMPUTER_USE_SYSTEM_PROMPT
        if enable_terminal:
            base_prompt = base_prompt + TERMINAL_PROMPT_ADDENDUM
        self._system_prompt = base_prompt

        self._event_queue: asyncio.Queue[ExecutorEvent] = asyncio.Queue()
        self._loop_task: asyncio.Task | None = None
        display = active_display(platform)
        if display is None:
            raise RuntimeError("No display is available")
        self._coords = CoordinateMapper(display, display_width, display_height)
        # Shared GUI action layer — same primitives are exposed to external
        # CLI agents via protean.mcp. This is the single source of
        # truth for "post-action screenshot" + detail-crop behavior.
        # ActionExecutor reads display_width/height from the mapper so
        # passing them again here would be redundant.
        self._actions = ActionExecutor(platform, self._coords)
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

        display = active_display(self._platform)
        if display is None:
            raise RuntimeError("No display is available")
        self._coords.select_display(display)

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
                        "content": self._result_to_anthropic(result),
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
            trailing_image_blocks: list[dict[str, Any]] = []

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

                tool_result_items.append({
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": self._result_text_for_openai(result),
                })
                # Only the last tool's images travel as a follow-up user
                # message — earlier batched tools are intentionally
                # text-only (matches the Anthropic path's
                # ``include_screenshot=is_last_tool`` contract).
                if is_last_tool:
                    trailing_image_blocks = self._result_images_for_openai(result)

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

            # Append the last tool's screenshots as a user message
            # (function_call_output is text-only).
            if trailing_image_blocks:
                messages.append({
                    "role": "user",
                    "content": trailing_image_blocks,
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
                    trimmed.append({
                        **msg,
                        "content": [{"type": text_type, "text": "[screenshot omitted]"}],
                    })
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
                        data = src["data"]
                        return {
                            **obj,
                            "source": {
                                **src,
                                "data": f"<base64 {len(data)} chars ~{len(data) * 3 // 4} bytes>",
                            },
                        }
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

    @staticmethod
    def _result_to_anthropic(r: ActionResult) -> list[dict[str, Any]]:
        """Translate an ActionResult into Anthropic ``tool_result`` content.

        Anthropic accepts an interleaved list of text and image blocks
        directly inside ``tool_result.content``. Order:
        text → screenshot → detail caption → detail crop.
        """
        blocks: list[dict[str, Any]] = []
        if r.text:
            blocks.append({"type": "text", "text": r.text})
        if r.screenshot_b64:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": r.screenshot_b64,
                },
            })
        if r.detail_caption:
            blocks.append({"type": "text", "text": r.detail_caption})
        if r.detail_crop_b64:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": r.detail_crop_b64,
                },
            })
        return blocks

    @staticmethod
    def _result_text_for_openai(r: ActionResult) -> str:
        """Collapse text + detail caption into one string.

        OpenAI's ``function_call_output`` only accepts a single ``output``
        string; images travel as a separate user message (see
        ``_result_images_for_openai``).
        """
        parts = [t for t in (r.text, r.detail_caption) if t]
        return "\n".join(parts) or "OK"

    @staticmethod
    def _result_images_for_openai(r: ActionResult) -> list[dict[str, Any]]:
        """OpenAI Responses ``input_image`` blocks for the result's screenshots.

        Returns both the full screenshot and the detail crop when present.
        Callers wrap these in a ``{"role": "user", "content": [...]}``
        message because OpenAI doesn't allow images inside
        ``function_call_output``.
        """
        blocks: list[dict[str, Any]] = []
        for b64 in (r.screenshot_b64, r.detail_crop_b64):
            if b64:
                blocks.append({
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{b64}",
                    "detail": "auto",
                })
        return blocks

    async def _execute_tool(
        self, name: str, input_data: dict[str, Any],
        *, include_screenshot: bool = True,
    ) -> ActionResult:
        """Execute a tool call and return its provider-neutral result.

        GUI actions delegate to ``ActionExecutor.dispatch``; the only
        tools handled here are the ones unique to this executor
        (``run_terminal_command`` / ``send_terminal_input``). ``done``
        is loop control and is handled by ``_run_anthropic_loop`` /
        ``_run_openai_loop`` directly, so it never reaches this method.

        Every GUI action automatically includes a post-action screenshot
        (driven by ``ActionExecutor``) so the model doesn't need to call
        screenshot separately after each action.
        """
        try:
            if name in GUI_TOOL_NAMES:
                return await self._actions.dispatch(
                    name, input_data, include_screenshot=include_screenshot,
                )

            elif name == "run_terminal_command":
                if not self._enable_terminal:
                    return ActionResult(
                        text="run_terminal_command is not enabled for this executor."
                    )
                cmd = input_data["command"]
                timeout = float(input_data.get("timeout_seconds", 30))
                shell = input_data.get("shell")
                terminal = await self._ensure_terminal()
                output = await terminal.start_command(cmd, timeout, shell)
                if not include_screenshot:
                    return ActionResult(text=output)
                await asyncio.sleep(0.5)
                return self._actions.screenshot_result(output)

            elif name == "send_terminal_input":
                if not self._enable_terminal:
                    return ActionResult(
                        text="send_terminal_input is not enabled for this executor."
                    )
                pid = int(input_data["pid"])
                text = input_data["input"]
                terminal = await self._ensure_terminal()
                output = await terminal.send_input(pid, text)
                if not include_screenshot:
                    return ActionResult(text=output)
                await asyncio.sleep(0.5)
                return self._actions.screenshot_result(output)

            else:
                return ActionResult(text=f"Unknown tool: {name}")

        except Exception as e:
            log.error("Tool %s failed: %s", name, e, exc_info=True)
            # Schema lookup stays here (rather than in actions.py) because
            # this executor's tool list includes done/terminal tools that
            # aren't part of GUI_TOOL_SPECS.
            tool = self._tools_by_name.get(name)
            schema = tool["input_schema"] if tool else None
            return ActionResult(text=format_tool_error(name, input_data, schema, e))

    # ── Helpers ──────────────────────────────────────────

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

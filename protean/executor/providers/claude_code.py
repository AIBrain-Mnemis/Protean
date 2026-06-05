"""Claude Code executor provider — backed by claude-agent-sdk.

Uses the official `claude-agent-sdk` to drive Claude Code. The SDK manages the
underlying CLI process, stream-json transport, session, and message parsing;
this module is just the adapter from SDK message types to ExecutorEvent and the
ExecutorProvider Protocol consumed by StepRunner.

Capabilities exposed to Claude:
  - Platform GUI tools via in-process MCP server (`mcp__protean__*`)
  - Claude Code built-in tools: Read, Write, Edit, Bash, Glob, Grep, WebFetch,
    WebSearch, TodoWrite, NotebookEdit, etc.
  - In-process `mcp__protean_ask__ask_user` tool that routes Claude's
    interactive questions to the AssistantChannel attached by StepRunner
    (replaces the built-in `AskUserQuestion`, which has no Python callback path
    in the SDK).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from protean.channels.base import AssistantChannel
from protean.executor import ExecutorContext, ExecutorEvent, ExecutorEventType, ExecutorProvider
from protean.executor.providers.prompts import CLAUDE_CODE_SYSTEM_PROMPT
from protean.mcp import build_mcp_server
from protean.platform.base import Platform, get_platform

log = logging.getLogger(__name__)

# ── Windows: force UTF-8 on subprocess text-mode pipes ──────────────────────
# claude-agent-sdk (and MCP CLIs it spawns) emit UTF-8 on stdout/stderr. On
# Chinese Windows, Python's locale codec is GBK, so when the SDK opens those
# pipes with text=True (no explicit encoding) the reader thread dies with
# `UnicodeDecodeError: 'gbk' codec can't decode byte 0x80...`. We patch
# subprocess.Popen once so any text-mode pipe without an explicit encoding
# defaults to UTF-8 with replacement on bad bytes — safe and reversible.
if sys.platform == "win32" and not getattr(
    subprocess.Popen, "_protean_utf8_patched", False
):
    _orig_popen_init = subprocess.Popen.__init__

    def _utf8_popen_init(self, *args, **kwargs):  # type: ignore[no-redef]
        wants_text = (
            kwargs.get("text")
            or kwargs.get("universal_newlines")
            or kwargs.get("encoding")
            or kwargs.get("errors")
        )
        if wants_text and not kwargs.get("encoding"):
            kwargs["encoding"] = "utf-8"
            kwargs.setdefault("errors", "replace")
        return _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _utf8_popen_init  # type: ignore[method-assign]
    subprocess.Popen._protean_utf8_patched = True  # type: ignore[attr-defined]


# ── Windows: force UTF-8 on subprocess text-mode pipes ──────────────────────
# claude-agent-sdk (and MCP CLIs it spawns) emit UTF-8 on stdout/stderr. On
# Chinese Windows, Python's locale codec is GBK, so when the SDK opens those
# pipes with text=True (no explicit encoding) the reader thread dies with
# `UnicodeDecodeError: 'gbk' codec can't decode byte 0x80...`. We patch
# subprocess.Popen once so any text-mode pipe without an explicit encoding
# defaults to UTF-8 with replacement on bad bytes — safe and reversible.
if sys.platform == "win32" and not getattr(
    subprocess.Popen, "_protean_utf8_patched", False
):
    _orig_popen_init = subprocess.Popen.__init__

    def _utf8_popen_init(self, *args, **kwargs):  # type: ignore[no-redef]
        wants_text = (
            kwargs.get("text")
            or kwargs.get("universal_newlines")
            or kwargs.get("encoding")
            or kwargs.get("errors")
        )
        if wants_text and not kwargs.get("encoding"):
            kwargs["encoding"] = "utf-8"
            kwargs.setdefault("errors", "replace")
        return _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _utf8_popen_init  # type: ignore[method-assign]
    subprocess.Popen._protean_utf8_patched = True  # type: ignore[attr-defined]


_BUILTIN_ALLOWED_TOOLS: list[str] = [
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
    "BashOutput",
    "KillShell",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "Task",
]

_PROTEAN_TOOL_GLOB = "mcp__protean__*"
_ASK_USER_TOOL = "mcp__protean_ask__ask_user"
_ASK_USER_GLOB = "mcp__protean_ask__*"

_DEFAULT_ALLOWED_TOOLS: list[str] = [
    _ASK_USER_GLOB,
    _PROTEAN_TOOL_GLOB,
    *_BUILTIN_ALLOWED_TOOLS,
]


class ClaudeCodeExecutor(ExecutorProvider):
    """Runs Claude Code via claude-agent-sdk.

    Usage:
        executor = ClaudeCodeExecutor()
        executor.set_assistant_channel(channel)   # optional
        await executor.start_task("Open Outlook and create a meeting")
        async for event in executor.get_events():
            print(event)
        await executor.close()
    """

    def __init__(
        self,
        mcp_config: dict[str, Any] | None = None,
        permission_mode: str = "bypassPermissions",
        allowed_tools: list[str] | None = None,
        working_dir: str | None = None,
        system_prompt: str = "",
        assistant_channel: AssistantChannel | None = None,
        max_buffer_size: int = 64 * 1024 * 1024,
        platform: Platform | None = None,
    ) -> None:
        self._platform = platform or get_platform()
        self._user_mcp_config = dict(mcp_config) if mcp_config else {}
        self._permission_mode = permission_mode
        self._allowed_tools = (
            allowed_tools if allowed_tools is not None else list(_DEFAULT_ALLOWED_TOOLS)
        )
        self._disallowed_tools: list[str] = ["AskUserQuestion"]
        self._working_dir = working_dir or str(Path.home())
        self._system_prompt = system_prompt or CLAUDE_CODE_SYSTEM_PROMPT
        self._max_buffer_size = max_buffer_size
        self._assistant_channel: AssistantChannel | None = assistant_channel
        self._client: ClaudeSDKClient | None = None
        self._event_queue: asyncio.Queue[ExecutorEvent] = asyncio.Queue()
        self._recv_task: asyncio.Task | None = None
        self._ask_server = self._build_ask_server()
        self._mcp_server = build_mcp_server(self._platform)

    # ------------------------------------------------------------ public API

    def set_assistant_channel(self, channel: AssistantChannel | None) -> None:
        """Attach (or detach) the AssistantChannel used by ask_user.

        Safe to call before or after start_task; the in-process MCP tool reads
        the live attribute every invocation.
        """
        self._assistant_channel = channel

    # ------------------------------------------------------------------ setup

    def _build_ask_server(self) -> Any:
        """In-process MCP server exposing one tool: ask_user.

        Routes the question (and optional screenshot bytes from a Read tool
        result) to the attached AssistantChannel. Returns the human's reply
        as the tool result so Claude can continue.
        """
        executor_self = self

        @tool(
            "ask_user",
            "Ask the human user a free-form question and wait for their reply. "
            "Use this whenever you need clarification, confirmation, or a "
            "decision that only the user can make. Do NOT use it for "
            "information you can obtain via other tools.",
            {"question": str},
        )
        async def ask_user(args: dict[str, Any]) -> dict[str, Any]:
            question = str(args.get("question", "")).strip()
            if not question:
                return {
                    "content": [
                        {"type": "text", "text": "Error: question is required."}
                    ],
                    "is_error": True,
                }
            channel = executor_self._assistant_channel
            if channel is None:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "No assistant channel is attached; cannot ask "
                                "the user. Proceed autonomously or stop and "
                                "report what's blocking you."
                            ),
                        }
                    ],
                    "is_error": True,
                }
            try:
                answer = await channel.ask(question)
            except Exception as e:  # noqa: BLE001
                log.warning("ask_user channel error: %s", e)
                return {
                    "content": [{"type": "text", "text": f"ask_user failed: {e}"}],
                    "is_error": True,
                }
            return {"content": [{"type": "text", "text": answer or ""}]}

        return create_sdk_mcp_server(
            name="protean_ask",
            version="0.1.0",
            tools=[ask_user],
        )

    def _build_options(self) -> ClaudeAgentOptions:
        mcp_servers: dict[str, Any] = {}
        if self._user_mcp_config:
            mcp_servers.update(self._user_mcp_config)
        mcp_servers["protean"] = self._mcp_server
        mcp_servers["protean_ask"] = self._ask_server
        return ClaudeAgentOptions(
            mcp_servers=mcp_servers,
            allowed_tools=self._allowed_tools,
            disallowed_tools=self._disallowed_tools,
            permission_mode=self._permission_mode,
            cwd=self._working_dir,
            max_buffer_size=self._max_buffer_size,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self._system_prompt,
            },
        )

    async def _ensure_client(self) -> None:
        """Connect (or reconnect) to the SDK client and start the receive loop."""
        if self._client is not None:
            return
        # Drain any leftover events from a previous session.
        while not self._event_queue.empty():
            self._event_queue.get_nowait()

        log.info("Starting Claude Agent SDK client")
        self._client = ClaudeSDKClient(options=self._build_options())
        try:
            await self._client.connect()
        except CLINotFoundError as e:
            self._client = None
            self._event_queue.put_nowait(
                ExecutorEvent(type=ExecutorEventType.ERROR, error=str(e))
            )
            raise
        self._recv_task = asyncio.create_task(self._receive_loop())

    # ----------------------------------------------------------- send helpers

    def _build_user_content(
        self,
        text: str,
        content_blocks: list[str | tuple[bytes, str]] | None,
    ) -> str | AsyncIterator[dict[str, Any]]:
        """Return either a plain string or a single-shot async iterator yielding
        one user message dict in the SDK's stream-json wire format."""
        if not content_blocks:
            return text

        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for block in content_blocks:
            if isinstance(block, str):
                content.append({"type": "text", "text": block})
            else:
                img_data, media_type = block
                b64 = base64.b64encode(img_data).decode("ascii")
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                })

        message = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

        async def _single_message() -> AsyncIterator[dict[str, Any]]:
            yield message

        return _single_message()

    # ------------------------------------------------------- ExecutorProvider

    async def start_task(
        self,
        instruction: str,
        context: str | ExecutorContext = "",
        content_blocks: list[str | tuple[bytes, str]] | None = None,
    ) -> None:
        """Start the SDK session and send the first message."""
        await self._ensure_client()
        self._drain_event_queue()
        context_str = str(context) if context else ""
        prompt_text = (
            f"Context: {context_str}\n\nTask: {instruction}" if context_str else instruction
        )
        payload = self._build_user_content(prompt_text, content_blocks)
        assert self._client is not None
        await self._client.query(payload)

    async def send_message(self, message: str) -> None:
        """Send a follow-up turn to the same session."""
        await self._ensure_client()
        self._drain_event_queue()
        assert self._client is not None
        await self._client.query(message)
        log.info("Sent follow-up to executor: %s", message[:100])

    def _drain_event_queue(self) -> None:
        """Clear any events left over from the previous turn.

        After an interrupt the SDK still delivers a ResultMessage for the
        cancelled turn after we've already synthesised our own DONE; that
        late message would otherwise sit in the queue and immediately end
        the next ``_collect_executor_result`` as a stale DONE.
        """
        while not self._event_queue.empty():
            self._event_queue.get_nowait()

    async def get_events(self) -> AsyncIterator[ExecutorEvent]:
        """Yield translated SDK events as ExecutorEvent objects."""
        while True:
            event = await self._event_queue.get()
            yield event
            if event.type in (ExecutorEventType.DONE, ExecutorEventType.ERROR):
                break

    async def interrupt(self) -> None:
        """Interrupt the current turn without tearing down the session.

        We ask the SDK to abort the in-flight turn, then push a synthetic
        DONE so the current ``_collect_executor_result`` exits promptly.
        The ``_recv_task`` runs ``receive_messages()`` and keeps reading
        for the next ``send_message`` query — no reset needed.
        """
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception as e:  # noqa: BLE001 — best-effort interrupt
                log.warning("Executor interrupt failed: %s", e)
        while not self._event_queue.empty():
            self._event_queue.get_nowait()
        self._event_queue.put_nowait(
            ExecutorEvent(type=ExecutorEventType.DONE, message="Interrupted")
        )
        log.info("Executor interrupted (session preserved)")

    async def close(self) -> None:
        """Disconnect the SDK client and tear down the receive loop."""
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:  # noqa: BLE001
                log.warning("Executor disconnect failed: %s", e)
            self._client = None

    # -------------------------------------------------------- receive / xlate

    async def _receive_loop(self) -> None:
        """Translate SDK messages into ExecutorEvent instances on the queue.

        Uses ``receive_messages()`` rather than ``receive_response()`` —
        the latter exits after each query's ResultMessage, which would
        kill the reader and leave subsequent ``send_message`` queries
        unread (next ``_collect_executor_result`` would hang). With
        ``receive_messages()`` the reader spans the entire session and
        every query's ResultMessage just emits a DONE event while the
        loop continues waiting for the next query.
        """
        assert self._client is not None
        try:
            async for msg in self._client.receive_messages():
                self._dispatch_message(msg)
        except asyncio.CancelledError:
            return
        except ClaudeSDKError as e:
            self._event_queue.put_nowait(
                ExecutorEvent(type=ExecutorEventType.ERROR, error=str(e))
            )
            await self._tear_down_after_fatal()
        except Exception as e:  # noqa: BLE001 — surface to caller as ERROR
            log.error("Executor receive loop error: %s", e)
            self._event_queue.put_nowait(
                ExecutorEvent(type=ExecutorEventType.ERROR, error=str(e))
            )
            await self._tear_down_after_fatal()

    async def _tear_down_after_fatal(self) -> None:
        """Drop the SDK client after its message reader died so the next
        start_task() reconnects instead of writing to a corpse."""
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:  # noqa: BLE001
                log.warning("Executor disconnect after fatal failed: %s", e)

    def _dispatch_message(self, msg: Any) -> None:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if block.text:
                        self._event_queue.put_nowait(ExecutorEvent(
                            type=ExecutorEventType.MESSAGE,
                            message=block.text,
                        ))
                elif isinstance(block, ToolUseBlock):
                    self._event_queue.put_nowait(ExecutorEvent(
                        type=ExecutorEventType.TOOL_CALL,
                        tool_name=block.name,
                        tool_args=block.input or {},
                    ))
        elif isinstance(msg, UserMessage):
            # Tool results come back as UserMessage with ToolResultBlock content.
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        result = block.content
                        if isinstance(result, list):
                            texts = [
                                b.get("text", "") for b in result
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            result_text = "\n".join(texts)
                        else:
                            result_text = str(result) if result is not None else ""
                        self._event_queue.put_nowait(ExecutorEvent(
                            type=ExecutorEventType.TOOL_RESULT,
                            result=result_text,
                        ))
        elif isinstance(msg, ResultMessage):
            self._event_queue.put_nowait(ExecutorEvent(
                type=ExecutorEventType.DONE,
                message=getattr(msg, "result", "") or "",
            ))

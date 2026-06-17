"""Task executor — LLM agent that operates the computer via MCP tools.

ExecutorProvider is the abstraction. Each provider (Claude Code, OpenAI, local)
implements it. The executor receives natural language instructions, decides
what GUI operations to perform, executes them, and returns results + trace.

## ExecutorProvider contract (the internal "executor protocol")

This module defines an in-process, async, streaming contract. It is the seam
where the daemon plugs a CUA backend (Claude Code, OpenAI Computer Use, a
future built-in, or a future ACP-remote adapter). Callers (`StepRunner`,
`TeachSession`) MUST be able to swap providers by name alone — so every
provider has to honor the same invariants:

1. `start_task` is non-blocking — it kicks off work and returns immediately.
2. `get_events` is the sole way to observe progress; it yields exactly one
   terminal event (DONE or ERROR) before completing. Implementations MUST
   guarantee `tool_call` events precede their matching `tool_result` events
   for the same logical action.
3. `send_message` is valid only AFTER `start_task` has been called and BEFORE
   `close`; it appends a turn to the same logical task and resumes streaming.
4. `interrupt` is idempotent and safe to call concurrently with `get_events`.
   After interrupt, the next terminal event yielded by `get_events` is DONE
   with a brief message (typical: "Interrupted").
5. `close` releases subprocess / network resources. Subsequent calls must be
   safe no-ops.
6. `assistant_channel` (if attached via `attach_assistant_channel`) is the
   ONE way for the executor to ask the human a free-form question. Provider
   implementations expose it to the model as a tool (e.g. `ask_user`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol, runtime_checkable

if TYPE_CHECKING:
    from protean.channels.base import AssistantChannel

# A single user-content block. Either a text fragment (str) or an inline image
# carried as (bytes, mime_type) — e.g. (jpeg_bytes, "image/jpeg"). Providers
# fan these out into their own SDK's multimodal format.
ContentBlock = str | tuple[bytes, str]


class ExecutorEventType(str, Enum):
    """Lifecycle events streamed from `ExecutorProvider.get_events`.

    Ordering invariants every provider must honor:

    - `TOOL_CALL` for a given physical action MUST precede its `TOOL_RESULT`.
    - Exactly one of `DONE` or `ERROR` is emitted per task as the final event.
    - `ITERATION` / `MESSAGE` may appear any number of times before the
      terminal event.
    """

    TOOL_CALL = "tool_call"      # model invoked a tool; payload in tool_name/tool_args
    TOOL_RESULT = "tool_result"  # tool returned; payload in tool_name/result
    MESSAGE = "message"          # assistant text turn (free-form)
    ITERATION = "iteration"      # heartbeat: model loop tick (optional)
    ERROR = "error"              # terminal failure; payload in error
    DONE = "done"                # terminal success; payload in message


@dataclass
class ExecutorEvent:
    """An event from the executor during task execution.

    Only a subset of fields is meaningful for any given `type`:

    - TOOL_CALL: tool_name, tool_args
    - TOOL_RESULT: tool_name, result
    - MESSAGE: message (and optionally reasoning)
    - ITERATION: message (e.g. "3"), input_tokens, output_tokens
    - ERROR: error
    - DONE: message (final summary)
    """

    type: ExecutorEventType
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    images: list[tuple[bytes, str, str]] = field(default_factory=list)
    message: str = ""
    error: str = ""
    reasoning: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ExecutorContext:
    """Structured context handed to the executor at task kickoff.

    Replaces ad-hoc context-string formatting at the call site. Each provider
    decides how to render the context for its model (system prompt, prefix
    message, ACP message parts, …). For backwards compatibility, ``__str__``
    produces the same flat layout that was previously built by
    ``TeachSession._build_executor_context`` — so a provider that simply does
    ``str(context)`` keeps behaving exactly as it did before.

    Field reference:
      - task: the high-level user goal for the whole session, if known.
      - completed_steps: a chronologically ordered, human-readable list of
        what has happened so far. Each entry is a single line, e.g.
        ``"1. [observed] open mail | action=cmd+space, type 'Mail'"``.
      - extra: optional free-form key/value lines a provider may render
        (e.g. ``{"working_dir": "/home/qi/Projects"}``).
    """

    task: str = ""
    completed_steps: list[str] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.task or self.completed_steps or self.extra)

    def __str__(self) -> str:
        parts: list[str] = []
        if self.task:
            parts.append(f"Task: {self.task}")
        if self.completed_steps:
            parts.append("Completed steps:")
            parts.extend(f"  {line}" for line in self.completed_steps)
        for key, value in self.extra.items():
            parts.append(f"{key}: {value}")
        return "\n".join(parts)


@runtime_checkable
class ExecutorProvider(Protocol):
    """Protocol for task execution backends.

    See module docstring for the full contract. The interface is intentionally
    minimal and async-streaming: any backend (in-process Claude Code, OpenAI
    Computer Use, built-in, remote ACP) can implement it.

    `set_assistant_channel` is OPTIONAL: providers that expose an interactive
    `ask_user` tool MAY implement it; callers should attach via the helper
    `attach_assistant_channel` rather than calling the method directly so that
    providers without the method are handled silently.
    """

    async def start_task(
        self,
        instruction: str,
        context: str | ExecutorContext = "",
        content_blocks: list[ContentBlock] | None = None,
    ) -> None:
        """Start executing a task. Non-blocking.

        Args:
            instruction: The task description (the imperative for THIS turn).
            context: Either a free-form string or a structured ExecutorContext.
                Providers may consume the structure directly or fall back to
                ``str(context)`` for the legacy flat layout.
            content_blocks: Optional interleaved text/image blocks from
                ``render_skill_for_llm``. Each element is either a str (text)
                or (image_bytes, mime_type).
        """
        ...

    async def send_message(self, message: str) -> None:
        """Send a follow-up message to the ongoing task (feedback, correction).

        Valid only after `start_task` and before `close`. Resumes streaming
        through the same `get_events` iterator (or a fresh one — provider's
        choice; see provider docs).
        """
        ...

    def get_events(self) -> AsyncIterator[ExecutorEvent]:
        """Stream execution events as they happen.

        Yields events until exactly one terminal event (DONE or ERROR) is
        produced, after which iteration ends.
        """
        ...

    async def interrupt(self) -> None:
        """Interrupt the current task immediately.

        Idempotent. After return, `get_events` will produce a DONE terminal
        event (typically with message "Interrupted").
        """
        ...

    async def close(self) -> None:
        """Clean up resources. Idempotent."""
        ...


def attach_assistant_channel(
    executor: ExecutorProvider, channel: AssistantChannel | None,
) -> bool:
    """Best-effort wire-up of an AssistantChannel onto an executor.

    Providers that expose an interactive ``ask_user`` tool implement an
    optional ``set_assistant_channel(channel)`` method (e.g. ClaudeCodeExecutor).
    This helper centralizes the duck-typed check so callers
    (StepRunner, TeachSession) don't each re-derive it.

    Returns True if the channel was attached, False otherwise.
    """
    setter = getattr(executor, "set_assistant_channel", None)
    if not callable(setter):
        return False
    try:
        setter(channel)
        return True
    except Exception:
        import logging
        logging.getLogger(__name__).debug(
            "Failed to attach assistant channel to executor", exc_info=True,
        )
        return False


def get_executor_provider(name: str, **kwargs: Any) -> ExecutorProvider:
    """Return an ExecutorProvider by name."""
    if name == "claude_code":
        from protean.executor.providers.claude_code import ClaudeCodeExecutor

        return ClaudeCodeExecutor(**kwargs)
    if name == "computer_use":
        from protean.executor.providers.computer_use import ComputerUseExecutor

        return ComputerUseExecutor(**kwargs)
    raise ValueError(f"Unknown executor provider: {name}")

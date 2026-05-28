"""Assistant channel abstraction — how the executor escalates to a human.

A teach session, CLI runner, or any other Protean entry point can attach an
``AssistantChannel`` to the executor. The executor's ``mcp__protean_ask__ask_user``
tool routes through that channel. Implementations live alongside this file
(``cli.py`` for the desktop dialog; the realtime bridge fulfils the same role
in voice sessions).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AssistantChannel(Protocol):
    """Protocol for asking an assistant (human or LLM) for help.

    Implementations:
      - CLIAssistantChannel: uses Platform.prompt_text() — the desktop dialog.
      - None: autonomous mode, no assistant available.

    Realtime voice sessions don't implement this directly; the bridge handles
    ``ask_user`` for the talker side.
    """

    async def ask(self, question: str, screenshot: bytes | None = None) -> str:
        """Ask a free-form question. Blocks until response."""
        ...

    async def confirm(self, message: str) -> bool:
        """Ask a yes/no question. Returns True for yes."""
        ...

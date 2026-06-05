"""Prompt templates used by executor providers.

One prompt per module, kept as plain strings so they can be diffed and edited
without touching provider logic.
"""

from protean.executor.providers.prompts.claude_code import CLAUDE_CODE_SYSTEM_PROMPT
from protean.executor.providers.prompts.computer_use import (
    COMPUTER_USE_SYSTEM_PROMPT,
    TERMINAL_PROMPT_ADDENDUM,
)

__all__ = [
    "CLAUDE_CODE_SYSTEM_PROMPT",
    "COMPUTER_USE_SYSTEM_PROMPT",
    "TERMINAL_PROMPT_ADDENDUM",
]

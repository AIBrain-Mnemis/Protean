"""LLM context-window and multimodal input budgeting."""

from __future__ import annotations

import os

DEFAULT_MAX_CONTEXT = 200_000
RESPONSE_TOKEN_RESERVE = 16_000
SYSTEM_TOKEN_RESERVE = 8_000
TOKENS_PER_CHAR = 1 / 3
TOKENS_PER_IMAGE = 1500
HARD_IMAGE_CAP = 100


class ContextOverflowError(Exception):
    """Provider rejected the request for exceeding the model context/input limit."""


def is_context_overflow(exc: BaseException) -> bool:
    msg = str(exc).lower()
    needles = (
        "context_length_exceeded",
        "prompt is too long",
        "maximum context length",
        "string too long",
        "request payload size",
        "payload too large",
        "request too large",
        "too many tokens",
        "input is too long",
        "exceeds the maximum",
    )
    if any(needle in msg for needle in needles):
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code in (413, "413"):
        return True
    if code in (400, "400") and ("token" in msg or "context" in msg):
        return True
    return False


def max_context() -> int:
    raw = os.getenv("PROTEAN_MAX_CONTEXT")
    if not raw:
        return DEFAULT_MAX_CONTEXT
    try:
        return max(20_000, int(raw))
    except ValueError:
        return DEFAULT_MAX_CONTEXT


def explicit_image_budget() -> int | None:
    raw = os.getenv("PROTEAN_GENERATE_IMAGE_BUDGET")
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def derive_image_budget(estimated_text_chars: int, max_context: int) -> int:
    text_tokens = int(estimated_text_chars * TOKENS_PER_CHAR)
    overhead = SYSTEM_TOKEN_RESERVE + RESPONSE_TOKEN_RESERVE
    available = max_context - overhead - text_tokens
    if available <= 0:
        return 0
    return min(HARD_IMAGE_CAP, available // TOKENS_PER_IMAGE)

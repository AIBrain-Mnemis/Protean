"""LLM provider defaults."""

from __future__ import annotations

from typing import Any

from protean.config import (
    DEFAULT_MODEL_ANTHROPIC,
    DEFAULT_MODEL_DOUBAO,
    DEFAULT_MODEL_GEMINI,
    DEFAULT_MODEL_OPENAI,
)

OPENAI_COMPATIBLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": DEFAULT_MODEL_OPENAI,
        "api": "responses",
    },
    "doubao": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": DEFAULT_MODEL_DOUBAO,
        "api": "chat",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": DEFAULT_MODEL_GEMINI,
        "api": "chat",
    },
}

ANTHROPIC_DEFAULTS: dict[str, str] = {
    "model": DEFAULT_MODEL_ANTHROPIC,
}

OPENAI_FALLBACK_DEFAULTS: dict[str, Any] = {
    "model": DEFAULT_MODEL_OPENAI,
}

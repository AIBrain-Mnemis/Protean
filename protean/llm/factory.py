"""LLM provider defaults and factory helpers."""

from __future__ import annotations

from typing import Any

import anthropic
import openai

from protean.config import DEFAULT_MODEL_OPENAI
from protean.llm.client import LLM
from protean.llm.providers import (
    ANTHROPIC_DEFAULTS,
    OPENAI_COMPATIBLE_DEFAULTS,
)


def create_llm(
    provider: str,
    config: dict[str, Any],
) -> LLM:
    return LLM(
        provider=provider,
        api_key=config["api_key"],
        model=config.get("model"),
        base_url=config.get("base_url"),
    )


def create_llm_from_config(
    llm_providers: dict[str, dict[str, Any]],
    default_provider: str,
    provider_override: str | None = None,
) -> LLM:
    name = provider_override or default_provider
    if name not in llm_providers:
        available = ", ".join(llm_providers.keys()) or "(none configured)"
        raise KeyError(f"LLM provider '{name}' not configured. Available: {available}")
    return create_llm(name, llm_providers[name])


def create_sync_client(
    provider: str,
    api_key: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> tuple[Any, str, bool]:
    is_anthropic = provider in ("anthropic", "claude") or (
        model and model.startswith("claude")
    )

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url

    if is_anthropic:
        resolved = model or ANTHROPIC_DEFAULTS["model"]
        return anthropic.Anthropic(**kwargs), resolved, False

    if not base_url:
        defaults = OPENAI_COMPATIBLE_DEFAULTS.get(provider, {})
        kwargs["base_url"] = defaults.get("base_url", "https://api.openai.com/v1")
    defaults = OPENAI_COMPATIBLE_DEFAULTS.get(provider, {})
    resolved = model or defaults.get("model", DEFAULT_MODEL_OPENAI)
    return openai.OpenAI(**kwargs), resolved, True

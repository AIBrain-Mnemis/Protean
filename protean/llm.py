"""LLM integration — thin wrapper over official SDKs.

Uses:
  - `openai` SDK for OpenAI, Doubao/Ark, Gemini (all OpenAI-compatible)
  - `anthropic` SDK for Anthropic/Claude

Only 2 dependencies, both officially maintained with security audits.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from protean.config import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_ANTHROPIC,
    DEFAULT_MODEL_DOUBAO,
    DEFAULT_MODEL_GEMINI,
    DEFAULT_MODEL_OPENAI,
)

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)


class ContextOverflowError(Exception):
    """Provider rejected the request for exceeding the model's context window.

    Normalized across Anthropic / OpenAI / Gemini error shapes so callers
    (e.g. SkillBuilder's degrade-and-retry loop) can react without
    substring-matching every provider's error format.
    """


def _is_context_overflow(exc: BaseException) -> bool:
    msg = str(exc).lower()
    needles = (
        "context_length_exceeded",
        "prompt is too long",
        "maximum context length",
        "string too long",
        "request payload size",
        "too many tokens",
        "input is too long",
        "exceeds the maximum",
    )
    if any(n in msg for n in needles):
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code in (400, "400") and ("token" in msg or "context" in msg):
        return True
    return False


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


# ── Provider registry ────────────────────────────────────

# Map of known providers to their OpenAI-compatible base URLs.
# Anthropic is handled separately via its own SDK.
_OPENAI_COMPATIBLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": DEFAULT_MODEL_OPENAI,
        "api": "responses",  # Responses API
    },
    "doubao": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": DEFAULT_MODEL_DOUBAO,
        "api": "chat",  # Chat Completions API
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": DEFAULT_MODEL_GEMINI,
        "api": "chat",
    },
}

_ANTHROPIC_DEFAULTS: dict[str, str] = {
    "model": DEFAULT_MODEL_ANTHROPIC,
}


def _build_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert our message format to Responses API input format."""
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        msg_images = msg.get("images")

        if role == "system":
            result.append({"role": "system", "content": content})
        elif isinstance(content, list):
            # Interleaved content parts: [{"type": "text", ...}, {"type": "image", ...}]
            parts: list[dict[str, Any]] = []
            for part in content:
                if part["type"] == "text":
                    parts.append({"type": "input_text", "text": part["text"]})
                elif part["type"] == "image":
                    b64 = base64.b64encode(part["data"]).decode("ascii")
                    parts.append({
                        "type": "input_image",
                        "image_url": f"data:{part['mime']};base64,{b64}",
                        "detail": "auto",
                    })
            result.append({"role": role, "content": parts})
        elif msg_images:
            parts = [{"type": "input_text", "text": content}]
            for img_data, media_type in msg_images:
                b64 = base64.b64encode(img_data).decode("ascii")
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{media_type};base64,{b64}",
                        "detail": "auto",
                    }
                )
            result.append({"role": role, "content": parts})
        else:
            result.append({"role": role, "content": content})
    return result


def _extract_responses_text(resp: Any) -> str:
    """Extract text content from a Responses API response."""
    for item in getattr(resp, "output", []):
        if getattr(item, "type", "") == "message":
            for part in getattr(item, "content", []):
                if getattr(part, "type", "") == "output_text":
                    return part.text
    return ""


def _parse_structured_text(content: str, response_model: type[BaseModel]) -> Any:
    """Parse JSON from a model response that may be wrapped in markdown
    fences or have a leading prose preamble. Some OpenAI-compatible servers
    ignore json_schema response_format and return ``## Header ... { ... }``
    or fenced ``` ```json blocks instead of raw JSON.
    """
    text = (content or "").strip()
    # Strip ``` fences if present.
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Try direct parse first.
    try:
        return response_model.model_validate_json(text)
    except Exception:
        pass
    # Fall back to extracting the largest balanced {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        return response_model.model_validate_json(candidate)
    # Re-raise the original error by attempting one more parse.
    return response_model.model_validate_json(text)


def _strict_json_schema(model: type[BaseModel]) -> dict:
    """Convert Pydantic schema to OpenAI strict mode.

    Strict mode requires:
    - additionalProperties: false on all objects
    - All properties in "required"
    - No $ref with sibling keywords
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    # Remove fields marked with exclude_from_llm (system-only fields like
    # metadata: dict[str, Any] which can't satisfy additionalProperties: false)
    if "properties" in schema:
        to_remove = [
            name
            for name, prop in schema["properties"].items()
            if isinstance(prop, dict) and prop.get("exclude_from_llm")
        ]
        for name in to_remove:
            del schema["properties"][name]

    def _resolve(obj: Any) -> Any:
        if isinstance(obj, dict):
            # Inline $ref
            if "$ref" in obj:
                ref_name = obj["$ref"].split("/")[-1]
                resolved = defs.get(ref_name, {}).copy()
                # Merge any sibling keys (like "description", "default") into resolved
                for k, v in obj.items():
                    if k != "$ref":
                        resolved[k] = v
                return _resolve(resolved)
            result = {}
            for k, v in obj.items():
                result[k] = _resolve(v)
            # Add strict constraints to objects
            if result.get("type") == "object" and "properties" in result:
                result["additionalProperties"] = False
                result["required"] = list(result["properties"].keys())
            return result
        if isinstance(obj, list):
            return [_resolve(item) for item in obj]
        return obj

    return _resolve(schema)


def _add_strict_props(obj: dict) -> None:
    """Add additionalProperties: false and make all properties required for strict mode."""
    if obj.get("type") == "object" and "properties" in obj:
        obj["additionalProperties"] = False
        obj["required"] = list(obj["properties"].keys())


def _build_openai_messages(
    messages: list[dict[str, Any]],
    images: list[tuple[bytes, str]] | None = None,
) -> list[dict[str, Any]]:
    """Convert our message format to OpenAI API format, handling images."""
    result = []
    image_count = 0

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        msg_images = msg.get("images")

        if isinstance(content, list):
            # Interleaved content parts
            parts: list[dict[str, Any]] = []
            for part in content:
                if part["type"] == "text":
                    parts.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image":
                    image_count += 1
                    b64 = base64.b64encode(part["data"]).decode("ascii")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{part['mime']};base64,{b64}"},
                    })
            result.append({"role": role, "content": parts})
        elif msg_images:
            # Multimodal message: text + images
            parts = [{"type": "text", "text": content}]
            for img_data, media_type in msg_images:
                image_count += 1
                b64 = base64.b64encode(img_data).decode("ascii")
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{b64}"},
                    }
                )
            result.append({"role": role, "content": parts})
        else:
            result.append({"role": role, "content": content})

    if image_count > 100:
        log.warning("payload contains %d images; provider may reject", image_count)
    return result


def _build_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert our message format to Anthropic API format. Returns (system, messages)."""
    system_text = ""
    api_messages = []
    image_count = 0

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        if role == "system":
            system_text = content
            continue

        if isinstance(content, list):
            # Interleaved content parts
            parts: list[dict[str, Any]] = []
            for part in content:
                if part["type"] == "text":
                    parts.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image":
                    image_count += 1
                    b64 = base64.b64encode(part["data"]).decode("ascii")
                    parts.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": part["mime"], "data": b64},
                    })
            api_messages.append({"role": role, "content": parts})
        else:
            msg_images = msg.get("images")
            if msg_images:
                parts = []
                for img_data, media_type in msg_images:
                    image_count += 1
                    b64 = base64.b64encode(img_data).decode("ascii")
                    parts.append(
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        }
                    )
                parts.append({"type": "text", "text": content})
                api_messages.append({"role": role, "content": parts})
            else:
                api_messages.append({"role": role, "content": content})

    if image_count > 100:
        log.warning("payload contains %d images; provider may reject", image_count)
    return system_text, api_messages


class LLM:
    """Unified LLM client backed by official SDKs.

    Usage:
        llm = LLM(provider="doubao", api_key="...", model="doubao-pro-256k")
        response = await llm.complete([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ])

        # With images:
        response = await llm.complete([
            {"role": "user", "content": "What's in this image?",
             "images": [(png_bytes, "image/png")]},
        ])
    """

    def __init__(
        self,
        provider: str,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.provider = provider
        self._model = model
        self._is_anthropic = provider in ("anthropic", "claude")

        if self._is_anthropic:
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._anthropic = anthropic.AsyncAnthropic(**kwargs)
            self._model = model or _ANTHROPIC_DEFAULTS["model"]
        else:
            defaults = _OPENAI_COMPATIBLE_DEFAULTS.get(
                provider, {"model": DEFAULT_MODEL_OPENAI},
            )
            self._openai = openai.AsyncOpenAI(
                api_key=api_key,
                base_url=base_url or defaults.get("base_url", "https://api.openai.com/v1"),
            )
            self._model = model or defaults.get("model", DEFAULT_MODEL_OPENAI)
            self._use_responses_api = defaults.get("api") == "responses"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 1,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        json_mode: bool = False,
        stream: bool = False,
    ) -> LLMResponse:
        """Send messages and get a completion. Handles text and images uniformly.

        Args:
            stream: Use streaming for Anthropic calls. Defaults to False.
                Enable for long-running or large-output requests to
                avoid proxy timeouts.
        """
        use_model = model or self._model

        if self._is_anthropic:
            return await self._complete_anthropic(
                messages,
                model=use_model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
            )
        elif self._use_responses_api:
            return await self._complete_responses(
                messages,
                model=use_model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            return await self._complete_openai(
                messages,
                model=use_model,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )

    async def complete_structured(
        self,
        messages: list[dict[str, Any]],
        response_model: type[T],
        *,
        model: str | None = None,
        temperature: float = 1,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        stream: bool = True,
    ) -> tuple[T, LLMResponse]:
        """Send messages and get a structured response validated by Pydantic.

        Uses OpenAI's structured output (response_format with json_schema)
        to guarantee the response conforms to the Pydantic model.

        For Anthropic, falls back to json_mode + Pydantic validation.

        Args:
            stream: Use streaming for Anthropic calls. Defaults to True.

        Returns (parsed_model, raw_response).
        """
        use_model = model or self._model

        if self._is_anthropic:
            # Anthropic: inject JSON schema into messages + validate with Pydantic
            schema = response_model.model_json_schema()
            schema_hint = (
                "\n\nYou MUST respond with ONLY valid JSON (no markdown fences) "
                "conforming to this exact JSON schema:\n"
                f"{json.dumps(schema, indent=2)}"
            )
            # Append schema hint to the last user message
            augmented = [m.copy() for m in messages]
            for m in reversed(augmented):
                if m.get("role") == "user":
                    if isinstance(m["content"], list):
                        m["content"] = m["content"] + [{"type": "text", "text": schema_hint}]
                    else:
                        m["content"] = m["content"] + schema_hint
                    break

            raw = await self._complete_anthropic(
                augmented,
                model=use_model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
            )

            content = raw.content.strip()
            # Strip markdown code fences if the LLM wrapped its JSON output
            if content.startswith("```"):
                # Remove opening ```json or ``` line and closing ```
                lines = content.split("\n")
                # Drop first line (```json) and last line (```)
                if lines[-1].strip() == "```":
                    lines = lines[1:-1]
                else:
                    lines = lines[1:]
                content = "\n".join(lines)

            # Retry once if JSON is invalid — feed error back to the LLM
            try:
                parsed = response_model.model_validate_json(content)
            except (json.JSONDecodeError, ValidationError) as e:
                log.warning("Structured output invalid, retrying: %s", e)
                augmented.append({"role": "assistant", "content": content or "(empty)"})
                augmented.append({"role": "user", "content": (
                    f"Your response was not valid JSON conforming to the schema. "
                    f"Error: {e}\n\nPlease output ONLY valid JSON, no other text."
                    f"{schema_hint}"
                )})
                raw = await self._complete_anthropic(
                    augmented,
                    model=use_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=stream,
                )
                content = raw.content.strip()
                if content.startswith("```"):
                    lines = content.split("\n")
                    if lines[-1].strip() == "```":
                        lines = lines[1:-1]
                    else:
                        lines = lines[1:]
                    content = "\n".join(lines)
                parsed = response_model.model_validate_json(content)

            return parsed, raw
        elif self._use_responses_api:
            # Responses API: use text.format=json_schema for structured output
            try:
                resp = await self._openai.responses.create(
                    model=use_model,
                    input=_build_responses_input(messages),
                    max_output_tokens=max_tokens,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "skill_output",
                            "schema": _strict_json_schema(response_model),
                            "strict": True,
                        }
                    },
                )
            except Exception as e:
                if _is_context_overflow(e):
                    raise ContextOverflowError(str(e)) from e
                raise
            content = _extract_responses_text(resp)
            raw = LLMResponse(
                content=content,
                model=resp.model,
                usage={
                    "prompt_tokens": getattr(resp.usage, "input_tokens", 0),
                    "completion_tokens": getattr(resp.usage, "output_tokens", 0),
                },
                raw=resp,
            )
            try:
                parsed = _parse_structured_text(content, response_model)
            except (json.JSONDecodeError, ValidationError) as e:
                # Some OpenAI-compatible servers ignore json_schema and return
                # markdown / prose. Retry once with an explicit instruction.
                log.warning(
                    "Responses API returned non-JSON, retrying with reminder: %s",
                    e,
                )
                schema_hint = (
                    f"\n\nSchema:\n{json.dumps(_strict_json_schema(response_model))}"
                )
                retry_input = _build_responses_input(messages) + [
                    {"role": "assistant", "content": content or "(empty)"},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON conforming "
                            "to the schema. Output ONLY a single JSON object that "
                            "matches the schema, no markdown, no prose."
                            + schema_hint
                        ),
                    },
                ]
                try:
                    resp = await self._openai.responses.create(
                        model=use_model,
                        input=retry_input,
                        max_output_tokens=max_tokens,
                        text={
                            "format": {
                                "type": "json_schema",
                                "name": "skill_output",
                                "schema": _strict_json_schema(response_model),
                                "strict": True,
                            }
                        },
                    )
                except Exception as e2:
                    if _is_context_overflow(e2):
                        raise ContextOverflowError(str(e2)) from e2
                    raise
                content = _extract_responses_text(resp)
                raw = LLMResponse(
                    content=content,
                    model=resp.model,
                    usage={
                        "prompt_tokens": getattr(resp.usage, "input_tokens", 0),
                        "completion_tokens": getattr(resp.usage, "output_tokens", 0),
                    },
                    raw=resp,
                )
                parsed = _parse_structured_text(content, response_model)
            return parsed, raw
        else:
            # OpenAI: native structured output
            try:
                resp = await self._openai.beta.chat.completions.parse(
                    model=use_model,
                    messages=_build_openai_messages(messages),
                    response_format=response_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as e:
                if _is_context_overflow(e):
                    raise ContextOverflowError(str(e)) from e
                raise
            choice = resp.choices[0]
            usage = resp.usage
            raw = LLMResponse(
                content=choice.message.content or "",
                model=resp.model,
                usage={
                    "prompt_tokens": usage.prompt_tokens if usage else 0,
                    "completion_tokens": usage.completion_tokens if usage else 0,
                },
                raw=resp,
            )
            parsed = choice.message.parsed
            if parsed is None:
                raise ValueError("Structured output parsing returned None")
            return parsed, raw

    async def _complete_openai(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _build_openai_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = await self._openai.chat.completions.create(**kwargs)
        except Exception as e:
            if _is_context_overflow(e):
                raise ContextOverflowError(str(e)) from e
            raise

        choice = resp.choices[0]
        usage = resp.usage
        return LLMResponse(
            content=choice.message.content or "",
            model=resp.model,
            usage={
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
            },
            raw=resp,
        )

    async def _complete_responses(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        try:
            resp = await self._openai.responses.create(
                model=model,
                input=_build_responses_input(messages),
                max_output_tokens=max_tokens,
            )
        except Exception as e:
            if _is_context_overflow(e):
                raise ContextOverflowError(str(e)) from e
            raise
        content = _extract_responses_text(resp)
        return LLMResponse(
            content=content,
            model=resp.model,
            usage={
                "prompt_tokens": getattr(resp.usage, "input_tokens", 0),
                "completion_tokens": getattr(resp.usage, "output_tokens", 0),
            },
            raw=resp,
        )

    async def _complete_anthropic(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool = True,
    ) -> LLMResponse:
        system_text, api_messages = _build_anthropic_messages(messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_text:
            kwargs["system"] = system_text

        _send = (
            self._stream_anthropic_messages
            if stream
            else self._send_anthropic_messages
        )
        try:
            resp = await _send(kwargs)
        except anthropic.BadRequestError as e:
            # Proxy may force extended thinking which requires temperature=1
            if "temperature" in str(e) and "thinking" in str(e) and temperature != 1:
                kwargs["temperature"] = 1
                try:
                    resp = await _send(kwargs)
                except Exception as inner:
                    if _is_context_overflow(inner):
                        raise ContextOverflowError(str(inner)) from inner
                    raise
            elif _is_context_overflow(e):
                raise ContextOverflowError(str(e)) from e
            else:
                raise
        except Exception as e:
            if _is_context_overflow(e):
                raise ContextOverflowError(str(e)) from e
            raise

        text_parts = [b.text for b in resp.content if b.type == "text"]
        content = "\n".join(text_parts)

        # Extended thinking can consume the entire max_tokens budget,
        # leaving no room for text output.  Retry with a larger budget.
        # Always stream the retry — large max_tokens may trigger proxy
        # timeouts on non-streaming requests.
        stop = getattr(resp, "stop_reason", None)
        has_thinking = any(b.type == "thinking" for b in resp.content)
        if has_thinking and (not content or stop == "max_tokens"):
            # 128000 is the max output cap on current Anthropic models
            # (e.g. claude-opus-4-6).  Going above will 400.
            expanded = 128000
            log.warning(
                "Anthropic thinking consumed budget — "
                "stop_reason=%s, block_types=%s, "
                "output_tokens=%d, max_tokens=%d → "
                "retrying with %d, model=%s",
                stop, [b.type for b in resp.content],
                resp.usage.output_tokens, max_tokens, expanded,
                resp.model,
            )
            kwargs["max_tokens"] = expanded
            resp = await self._stream_anthropic_messages(kwargs)
            text_parts = [b.text for b in resp.content if b.type == "text"]
            content = "\n".join(text_parts)
            stop = getattr(resp, "stop_reason", None)

        return LLMResponse(
            content=content,
            model=resp.model,
            usage={
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
            },
            raw=resp,
        )

    async def _send_anthropic_messages(
        self,
        kwargs: dict[str, Any],
    ) -> anthropic.types.Message:
        """Non-streaming Anthropic messages.create."""
        return await self._anthropic.messages.create(**kwargs)

    async def _stream_anthropic_messages(
        self,
        kwargs: dict[str, Any],
    ) -> anthropic.types.Message:
        """Streaming Anthropic messages.create, returns the final Message.

        More robust against proxy timeouts for long-running or
        large-output requests (extended thinking, large max_tokens).
        """
        async with self._anthropic.messages.stream(**kwargs) as stream:
            return await stream.get_final_message()


def create_llm(
    provider: str,
    config: dict[str, Any],
) -> LLM:
    """Create an LLM instance from a provider name and config dict.

    Config keys: api_key (required), model, base_url.
    """
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
    """Create an LLM from the full app config.

    Args:
        llm_providers: Dict of provider_name -> config.
        default_provider: Which provider to use by default.
        provider_override: Optional override (e.g. from CLI --provider flag).
    """
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
    """Create a synchronous LLM client for use in executor backends.

    Returns:
        (client, resolved_model, is_openai) tuple.
        - client: openai.OpenAI or anthropic.Anthropic instance
        - resolved_model: model name with defaults applied
        - is_openai: True if OpenAI-compatible, False if Anthropic
    """
    is_anthropic = provider in ("anthropic", "claude") or (
        model and model.startswith("claude")
    )

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url

    if is_anthropic:
        resolved = model or _ANTHROPIC_DEFAULTS["model"]
        return anthropic.Anthropic(**kwargs), resolved, False

    if not base_url:
        defaults = _OPENAI_COMPATIBLE_DEFAULTS.get(provider, {})
        kwargs["base_url"] = defaults.get("base_url", "https://api.openai.com/v1")
    defaults = _OPENAI_COMPATIBLE_DEFAULTS.get(provider, {})
    resolved = model or defaults.get("model", DEFAULT_MODEL_OPENAI)
    return openai.OpenAI(**kwargs), resolved, True

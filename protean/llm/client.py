"""LLM integration — thin wrapper over official SDKs.

Uses:
  - `openai` SDK for OpenAI, Doubao/Ark, Gemini (all OpenAI-compatible)
  - `anthropic` SDK for Anthropic/Claude

Only 2 dependencies, both officially maintained with security audits.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from protean.config import DEFAULT_MAX_TOKENS
from protean.llm.context import ContextOverflowError, is_context_overflow
from protean.llm.messages import (
    build_anthropic_messages,
    build_openai_messages,
    build_responses_input,
    extract_responses_text,
    parse_structured_text,
    strict_json_schema,
)
from protean.llm.providers import (
    ANTHROPIC_DEFAULTS,
    OPENAI_COMPATIBLE_DEFAULTS,
    OPENAI_FALLBACK_DEFAULTS,
)

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)


def _looks_like_bad_request(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code == 400:
        return True
    text = str(error).lower()
    return "400 bad request" in text or "badrequest" in text


def _looks_like_response_format_rejection(error: Exception) -> bool:
    if not _looks_like_bad_request(error):
        return False
    text = str(error).lower()
    # Some OpenAI-compatible gateways surface only a generic HTTP 400 line
    # even when the rejected field is response_format. Context-overflow 400s
    # are handled before this helper is called, so a single compatibility
    # retry is the most useful behavior for opaque Bad Request responses.
    if getattr(error, "status_code", None) == 400:
        return True
    if text.strip() in {"", "bad request"} or "400 bad request" in text:
        return True
    markers = (
        "response_format",
        "json_schema",
        "json_object",
        "schema",
        "structured output",
    )
    return any(marker in text for marker in markers)


def _append_json_schema_hint(
    messages: list[dict[str, Any]],
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    schema_hint = (
        "\n\nYou MUST respond with ONLY one valid JSON object, no markdown "
        "fences and no prose. The JSON object must conform to this schema:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    augmented = [m.copy() for m in messages]
    for msg in reversed(augmented):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            msg["content"] = content + [{"type": "text", "text": schema_hint}]
        else:
            msg["content"] = str(content) + schema_hint
        break
    else:
        augmented.append({"role": "user", "content": schema_hint})
    return augmented


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


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
        self._model: str
        self._is_anthropic = provider in ("anthropic", "claude")
        self._anthropic: Any = None
        self._openai: Any = None

        if self._is_anthropic:
            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._anthropic = anthropic.AsyncAnthropic(**kwargs)
            self._model = model or ANTHROPIC_DEFAULTS["model"]
        else:
            defaults = OPENAI_COMPATIBLE_DEFAULTS.get(provider, OPENAI_FALLBACK_DEFAULTS)
            self._openai = openai.AsyncOpenAI(
                api_key=api_key,
                base_url=base_url or defaults.get("base_url", "https://api.openai.com/v1"),
            )
            self._model = model or str(defaults["model"])
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
                    input=build_responses_input(messages),
                    max_output_tokens=max_tokens,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "skill_output",
                            "schema": strict_json_schema(response_model),
                            "strict": True,
                        }
                    },
                )
            except Exception as e:
                if is_context_overflow(e):
                    raise ContextOverflowError(str(e)) from e
                raise
            content = extract_responses_text(resp)
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
                parsed = parse_structured_text(content, response_model)
            except (json.JSONDecodeError, ValidationError) as e:
                # Some OpenAI-compatible servers ignore json_schema and return
                # markdown / prose. Retry once with an explicit instruction.
                log.warning(
                    "Responses API returned non-JSON, retrying with reminder: %s",
                    e,
                )
                schema_hint = (
                    f"\n\nSchema:\n{json.dumps(strict_json_schema(response_model))}"
                )
                retry_input = build_responses_input(messages) + [
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
                                "schema": strict_json_schema(response_model),
                                "strict": True,
                            }
                        },
                    )
                except Exception as e2:
                    if is_context_overflow(e2):
                        raise ContextOverflowError(str(e2)) from e2
                    raise
                content = extract_responses_text(resp)
                raw = LLMResponse(
                    content=content,
                    model=resp.model,
                    usage={
                        "prompt_tokens": getattr(resp.usage, "input_tokens", 0),
                        "completion_tokens": getattr(resp.usage, "output_tokens", 0),
                    },
                    raw=resp,
                )
                parsed = parse_structured_text(content, response_model)
            return parsed, raw
        else:
            # OpenAI-compatible Chat Completions: pass Protean's cleaned schema
            # explicitly. The SDK's Pydantic parse helper includes internal
            # fields such as dict[str, bytes] figure_data, which some compatible
            # providers reject during schema validation.
            schema = strict_json_schema(response_model)
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "skill_output",
                    "schema": schema,
                    "strict": True,
                },
            }

            async def _create_chat_completion(
                api_messages: list[dict[str, Any]],
                mode: str,
            ) -> Any:
                kwargs: dict[str, Any] = {
                    "model": use_model,
                    "messages": api_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if mode == "json_schema":
                    kwargs["response_format"] = response_format
                elif mode == "json_object":
                    kwargs["response_format"] = {"type": "json_object"}
                return await self._openai.chat.completions.create(**kwargs)

            mode = "json_schema"
            api_messages = build_openai_messages(messages)
            try:
                resp = await _create_chat_completion(api_messages, mode)
            except Exception as e:
                if is_context_overflow(e):
                    raise ContextOverflowError(str(e)) from e
                if not _looks_like_response_format_rejection(e):
                    raise
                log.warning(
                    "Provider rejected json_schema response_format; "
                    "falling back to JSON-mode schema prompt: %s",
                    e,
                )
                mode = "json_object"
                api_messages = build_openai_messages(
                    _append_json_schema_hint(messages, schema)
                )
                try:
                    resp = await _create_chat_completion(api_messages, mode)
                except Exception as e2:
                    if is_context_overflow(e2):
                        raise ContextOverflowError(str(e2)) from e2
                    if not _looks_like_response_format_rejection(e2):
                        raise
                    log.warning(
                        "Provider rejected json_object response_format; "
                        "falling back to schema prompt without response_format: %s",
                        e2,
                    )
                    mode = "schema_prompt"
                    resp = await _create_chat_completion(api_messages, mode)
            choice = resp.choices[0]
            usage = resp.usage
            content = choice.message.content or ""
            raw = LLMResponse(
                content=content,
                model=resp.model,
                usage={
                    "prompt_tokens": usage.prompt_tokens if usage else 0,
                    "completion_tokens": usage.completion_tokens if usage else 0,
                },
                raw=resp,
            )
            try:
                parsed = parse_structured_text(content, response_model)
            except (json.JSONDecodeError, ValidationError) as e:
                log.warning(
                    "Chat Completions returned non-JSON, retrying with reminder: %s",
                    e,
                )
                retry_messages = build_openai_messages(messages) + [
                    {"role": "assistant", "content": content or "(empty)"},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON conforming "
                            "to the schema. Output ONLY a single JSON object that "
                            "matches the schema, no markdown, no prose."
                            f"\n\nSchema:\n{json.dumps(schema, ensure_ascii=False)}"
                        ),
                    },
                ]
                if mode != "json_schema":
                    retry_messages = api_messages + [
                        {"role": "assistant", "content": content or "(empty)"},
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was not valid JSON "
                                "conforming to the schema. Output ONLY a single "
                                "JSON object that matches the schema, no "
                                "markdown, no prose."
                            ),
                        },
                    ]
                try:
                    resp = await _create_chat_completion(retry_messages, mode)
                except Exception as e2:
                    if is_context_overflow(e2):
                        raise ContextOverflowError(str(e2)) from e2
                    raise
                choice = resp.choices[0]
                usage = resp.usage
                content = choice.message.content or ""
                raw = LLMResponse(
                    content=content,
                    model=resp.model,
                    usage={
                        "prompt_tokens": usage.prompt_tokens if usage else 0,
                        "completion_tokens": usage.completion_tokens if usage else 0,
                    },
                    raw=resp,
                )
                parsed = parse_structured_text(content, response_model)
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
            "messages": build_openai_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = await self._openai.chat.completions.create(**kwargs)
        except Exception as e:
            if is_context_overflow(e):
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
                input=build_responses_input(messages),
                max_output_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            if is_context_overflow(e):
                raise ContextOverflowError(str(e)) from e
            raise
        content = extract_responses_text(resp)
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
        system_text, api_messages = build_anthropic_messages(messages)

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
                    if is_context_overflow(inner):
                        raise ContextOverflowError(str(inner)) from inner
                    raise
            elif is_context_overflow(e):
                raise ContextOverflowError(str(e)) from e
            else:
                raise
        except Exception as e:
            if is_context_overflow(e):
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

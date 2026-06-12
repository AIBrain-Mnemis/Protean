"""Provider message conversion and structured-output helpers."""

from __future__ import annotations

import base64
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)


def build_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        msg_images = msg.get("images")

        if role == "system":
            result.append({"role": "system", "content": content})
        elif isinstance(content, list):
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
                parts.append({
                    "type": "input_image",
                    "image_url": f"data:{media_type};base64,{b64}",
                    "detail": "auto",
                })
            result.append({"role": role, "content": parts})
        else:
            result.append({"role": role, "content": content})
    return result


def extract_responses_text(resp: Any) -> str:
    for item in getattr(resp, "output", []):
        if getattr(item, "type", "") == "message":
            for part in getattr(item, "content", []):
                if getattr(part, "type", "") == "output_text":
                    return part.text
    return ""


def parse_structured_text(content: str, response_model: type[BaseModel]) -> Any:
    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return response_model.model_validate_json(text)
    except ValidationError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        return response_model.model_validate_json(candidate)
    return response_model.model_validate_json(text)


def strict_json_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

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
            if "$ref" in obj:
                ref_name = obj["$ref"].split("/")[-1]
                resolved = defs.get(ref_name, {}).copy()
                for key, value in obj.items():
                    if key != "$ref":
                        resolved[key] = value
                return _resolve(resolved)
            result = {}
            for key, value in obj.items():
                result[key] = _resolve(value)
            if result.get("type") == "object" and "properties" in result:
                result["additionalProperties"] = False
                result["required"] = list(result["properties"].keys())
            return result
        if isinstance(obj, list):
            return [_resolve(item) for item in obj]
        return obj

    return _resolve(schema)


def build_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    image_count = 0

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        msg_images = msg.get("images")

        if isinstance(content, list):
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
            parts = [{"type": "text", "text": content}]
            for img_data, media_type in msg_images:
                image_count += 1
                b64 = base64.b64encode(img_data).decode("ascii")
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{b64}"},
                })
            result.append({"role": role, "content": parts})
        else:
            result.append({"role": role, "content": content})

    if image_count > 100:
        log.warning("payload contains %d images; provider may reject", image_count)
    return result


def build_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
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
            parts: list[dict[str, Any]] = []
            for part in content:
                if part["type"] == "text":
                    parts.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image":
                    image_count += 1
                    b64 = base64.b64encode(part["data"]).decode("ascii")
                    parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part["mime"],
                            "data": b64,
                        },
                    })
            api_messages.append({"role": role, "content": parts})
        else:
            msg_images = msg.get("images")
            if msg_images:
                parts = []
                for img_data, media_type in msg_images:
                    image_count += 1
                    b64 = base64.b64encode(img_data).decode("ascii")
                    parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    })
                parts.append({"type": "text", "text": content})
                api_messages.append({"role": role, "content": parts})
            else:
                api_messages.append({"role": role, "content": content})

    if image_count > 100:
        log.warning("payload contains %d images; provider may reject", image_count)
    return system_text, api_messages

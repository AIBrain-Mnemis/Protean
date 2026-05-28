"""Configuration management for Protean."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# ── Default model names (single source of truth) ─────────
# Used only when the corresponding *_MODEL env var is unset.
# Pick generally-available public model aliases here; users override per
# provider via .env (OPENAI_MODEL, ANTHROPIC_MODEL, GEMINI_MODEL, ARK_MODEL).
DEFAULT_MODEL_OPENAI = "gpt-5"
DEFAULT_MODEL_ANTHROPIC = "claude-sonnet-4-5"
DEFAULT_MODEL_GEMINI = "gemini-3.5-flash"
DEFAULT_MODEL_DOUBAO = "doubao-pro-256k"
DEFAULT_MAX_TOKENS = 32768


def _data_dir() -> Path:
    """Return the data directory for Protean.

    Defaults to ``<project_root>/data/`` (next to ``pyproject.toml``) so that
    recordings, skills, and other artefacts live alongside the source code —
    convenient during development.  Override with ``PROTEAN_DATA_DIR``.
    """
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "data"


@dataclass
class ProteanConfig:
    """Top-level configuration."""

    # Paths
    data_dir: Path = field(default_factory=_data_dir)
    skills_dir: Path = field(default_factory=lambda: _data_dir() / "skills")
    recordings_dir: Path = field(default_factory=lambda: _data_dir() / "recordings")

    # LLM
    default_provider: str = "openai"
    llm_providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    realtime_model: str | None = None
    skill_max_tokens: int = DEFAULT_MAX_TOKENS
    skill_temperature: float = 1.0

    # Executor
    image_keep_last: int = 10

    # Computer Use Agent (CUA)
    cua_enable_terminal: bool = True
    cua_terminal_command: list[str] | None = None

    @classmethod
    def load(cls, env_file: Path | None = None) -> ProteanConfig:
        """Load config from environment variables and .env file.

        Loads .env from the project root (next to pyproject.toml).
        """
        if env_file:
            load_dotenv(env_file, override=True)
        else:
            # Project root: where pyproject.toml lives
            project_root = Path(__file__).resolve().parent.parent
            dotenv_path = project_root / ".env"
            if dotenv_path.exists():
                load_dotenv(dotenv_path, override=True)

        providers: dict[str, dict[str, Any]] = {}

        # OpenAI
        if os.getenv("OPENAI_API_KEY"):
            providers["openai"] = {
                "api_key": os.environ["OPENAI_API_KEY"],
                "model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL_OPENAI),
                "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            }

        # Anthropic
        if os.getenv("ANTHROPIC_API_KEY"):
            providers["anthropic"] = {
                "api_key": os.environ["ANTHROPIC_API_KEY"],
                "model": os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL_ANTHROPIC),
                "base_url": os.getenv("ANTHROPIC_BASE_URL") or None,
            }

        # Gemini
        if os.getenv("GEMINI_API_KEY"):
            providers["gemini"] = {
                "api_key": os.environ["GEMINI_API_KEY"],
                "model": os.getenv("GEMINI_MODEL", DEFAULT_MODEL_GEMINI),
            }

        # Doubao (火山引擎)
        if os.getenv("ARK_API_KEY"):
            providers["doubao"] = {
                "api_key": os.environ["ARK_API_KEY"],
                "model": os.getenv("ARK_MODEL", DEFAULT_MODEL_DOUBAO),
                "base_url": os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            }

        default = os.getenv("PROTEAN_DEFAULT_PROVIDER", "")
        if not default:
            # Auto-detect: prefer doubao if ARK_API_KEY is set
            if "doubao" in providers:
                default = "doubao"
            elif providers:
                default = next(iter(providers))
            else:
                default = "openai"

        return cls(
            data_dir=Path(os.getenv("PROTEAN_DATA_DIR", str(_data_dir()))),
            skills_dir=Path(os.getenv("PROTEAN_SKILLS_DIR", str(_data_dir() / "skills"))),
            recordings_dir=Path(
                os.getenv("PROTEAN_RECORDINGS_DIR", str(_data_dir() / "recordings"))
            ),
            default_provider=default,
            llm_providers=providers,
            realtime_model=os.getenv("PROTEAN_REALTIME_MODEL") or None,
            cua_enable_terminal=_parse_bool_env("PROTEAN_CUA_TERMINAL", True),
            cua_terminal_command=(
                os.environ["PROTEAN_CUA_TERMINAL_CMD"].split()
                if os.getenv("PROTEAN_CUA_TERMINAL_CMD") else None
            ),
        )


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

"""Install Protean agent skills into local agent runtimes."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import tomlkit

from protean.skills.bootstrap import (
    AGENT_PROTEAN_SKILL_NAME,
    build_and_evolve_skills_with_protean_skill,
)
from protean.skills.registry import SkillRegistry
from protean.skills.renderer import render_skill

if TYPE_CHECKING:
    from collections.abc import Iterable

    from protean.skills.schema import Skill


# MCP server name. The single source of truth for the wire-format tool
# prefix (``mcp__protean__*``), the in-process key in
# ``ClaudeAgentOptions.mcp_servers``, and the user-facing config key in
# the external CLI agent's MCP config (codex ``mcp_servers.protean`` /
# claude_code ``mcpServers.protean``). Must match
# ``create_sdk_mcp_server(name=...)`` in ``protean/mcp/server.py``.
_MCP_SERVER_NAME = "protean"


@dataclass(frozen=True)
class AgentTarget:
    display_name: str
    source: str
    skills_dir: Path
    # User-level instruction file the runtime auto-loads on session start
    # (Codex: AGENTS.md, Claude Code: CLAUDE.md). None = runtime has no such
    # convention, so instruction injection is skipped.
    instructions_file: Path | None = None
    # User-level MCP server config the runtime reads on startup.
    # Codex: ~/.codex/config.toml (TOML), Claude Code: ~/.claude.json (JSON).
    # None = runtime has no such convention.
    mcp_config_file: Path | None = None
    mcp_config_format: str | None = None  # "toml" | "json"


@dataclass(frozen=True)
class AgentSetupResult:
    target: AgentTarget
    bootstrap_path: Path
    copied_skills: list[Path]
    instructions_path: Path | None
    mcp_config_path: Path | None


@dataclass(frozen=True)
class AgentUninstallResult:
    target: AgentTarget
    # Bootstrap skill folder we removed (None if it wasn't installed).
    removed_bootstrap: Path | None
    # Protean-managed skill folders we removed (excluding the bootstrap).
    removed_skills: list[Path]
    # Instructions file we touched (None if no file or no managed block).
    # ``instructions_file_deleted`` is True when we removed the file because
    # nothing but the managed block was left in it.
    instructions_path: Path | None
    instructions_file_deleted: bool
    # MCP config file we touched (None if no file or no managed entry).
    mcp_config_path: Path | None


def resolve_agent_target(kind: str) -> AgentTarget:
    normalized = kind.strip().lower().replace("-", "_")
    if normalized == "codex":
        home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        return AgentTarget(
            display_name="Codex",
            source="codex",
            skills_dir=home / "skills",
            instructions_file=home / "AGENTS.md",
            mcp_config_file=home / "config.toml",
            mcp_config_format="toml",
        )
    if normalized in {"claude", "claude_code"}:
        home = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude"))).expanduser()
        # Claude Code stores its MCP config in a sibling JSON file
        # (``~/.claude.json`` for the default home). Deriving by sibling
        # name lets a custom CLAUDE_HOME stay self-contained instead of
        # always pointing back at the real ``~/.claude.json``.
        mcp_path = home.parent / f"{home.name}.json"
        return AgentTarget(
            display_name="Claude Code",
            source="claude_code",
            skills_dir=home / "skills",
            instructions_file=home / "CLAUDE.md",
            mcp_config_file=mcp_path,
            mcp_config_format="json",
        )
    raise ValueError(f"Unsupported agent target: {kind}")


def install_bootstrap_skill(
    target: AgentTarget,
    skill: "Skill",
) -> Path:
    return render_skill(skill, target.skills_dir / skill.name)


def upsert_sentinel_block(
    path: Path,
    *,
    begin: str,
    end: str,
    block: str,
) -> None:
    """Write ``block`` (which must include ``begin`` and ``end``) into ``path``.

    Behavior:
    - If the file contains ``begin`` and ``end`` (with begin first), replace
      everything between (and including) them with ``block``. A single
      newline immediately after the old ``end`` is swallowed so re-runs
      don't accumulate blank lines.
    - If the file exists but has no sentinels, append ``block`` separated
      by a blank line.
    - If the file is missing or empty, write ``block`` as the whole file.
    - The resulting file always ends with a single trailing newline.
    - No write happens if the content would be unchanged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    begin_idx = existing.find(begin)
    end_idx = existing.find(end)
    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        end_idx += len(end)
        if end_idx < len(existing) and existing[end_idx] == "\n":
            end_idx += 1
        updated = existing[:begin_idx] + block + existing[end_idx:]
    elif existing.strip():
        updated = f"{existing.rstrip()}\n\n{block}"
    else:
        updated = block

    if not updated.endswith("\n"):
        updated += "\n"

    if updated != existing:
        path.write_text(updated, encoding="utf-8")


def remove_sentinel_block(path: Path, *, begin: str, end: str) -> bool:
    """Remove the block delimited by ``begin``/``end`` from ``path``.

    Returns True if a block was found and removed, False otherwise.
    The file is left empty (not deleted) when nothing remains after
    removal; callers can decide whether to unlink.
    """
    existing = path.read_text(encoding="utf-8")
    begin_idx = existing.find(begin)
    end_idx = existing.find(end)
    if begin_idx == -1 or end_idx == -1 or end_idx <= begin_idx:
        return False

    end_idx += len(end)
    if end_idx < len(existing) and existing[end_idx] == "\n":
        end_idx += 1
    updated = (existing[:begin_idx].rstrip() + "\n" + existing[end_idx:]).lstrip("\n")
    if updated.strip():
        if not updated.endswith("\n"):
            updated += "\n"
        path.write_text(updated, encoding="utf-8")
    else:
        path.write_text("", encoding="utf-8")
    return True


# Sentinel markers delimit the Protean-managed region in the agent's
# instruction file. Anything between them is owned by `agents setup`
# and overwritten on every run. Anything outside is left untouched.
_INSTRUCTIONS_BLOCK_BEGIN = "<!-- protean:begin -->"
_INSTRUCTIONS_BLOCK_END = "<!-- protean:end -->"

# `{skill_name}` is filled in via str.format at injection time.
_INSTRUCTIONS_BLOCK_TEMPLATE = """\
At the start of each new chat, load the `{skill_name}` skill and treat it as active for the session.
Use the loaded skill as the source of truth for when Protean should be used.
In practice, Protean covers:
- recording a screen demonstration and turning it into a skill
- realtime voice or screen-share teaching via the daemon
- running a zero-shot task prompt
- hand-editing or importing a SKILL.md
- replaying a skill with self-refinement
- evolving the current agent session's trajectory into a reusable skill

Invoking the Protean CLI: prefer the bare `protean` command. If the shell reports "command not found", retry with the canonical shim path written by Protean's installer:
- POSIX (macOS / Linux): `~/.local/bin/protean`
- Windows (PowerShell / cmd): `%USERPROFILE%\\.local\\bin\\protean.cmd`
This applies to every `protean ...` invocation (`skills`, `record`, `generate`, `daemon`, `trajectories`, etc.).
"""  # noqa: E501


def inject_instructions(target: AgentTarget, skill: "Skill") -> Path | None:
    """Insert or refresh the Protean block in the agent's instruction file.

    Returns the path written, or ``None`` when the target has no
    instructions-file convention.
    """
    path = target.instructions_file
    if path is None:
        return None

    body = _INSTRUCTIONS_BLOCK_TEMPLATE.format(skill_name=skill.name)
    block = f"{_INSTRUCTIONS_BLOCK_BEGIN}\n{body}\n{_INSTRUCTIONS_BLOCK_END}\n"
    upsert_sentinel_block(
        path,
        begin=_INSTRUCTIONS_BLOCK_BEGIN,
        end=_INSTRUCTIONS_BLOCK_END,
        block=block,
    )
    return path


# Sentinel comments wrap the Protean MCP entry in TOML configs. JSON
# configs use a single object key (``mcpServers.protean``) so no
# sentinel is needed there.
# (TOML uses tomlkit for structured edits and does not need sentinels.)


def _mcp_command_args() -> tuple[str, list[str]]:
    """Return the command + args to launch the Protean MCP server.

    Uses the absolute path of the current interpreter with ``-m protean``
    rather than the bare ``protean`` shim. Agent runtimes (e.g. Codex
    Desktop) spawn MCP servers with a minimal PATH that excludes
    ``~/.local/bin``, so the shim's ``exec uv`` fails with
    ``uv: not found``. An absolute interpreter path needs no PATH lookup
    and no ``uv``.
    """
    if sys.executable:
        return sys.executable, ["-m", "protean", "mcp"]
    return "protean", ["mcp"]


def _mcp_toml_table() -> "tomlkit.items.Table":
    command, args = _mcp_command_args()
    table = tomlkit.table()
    table["command"] = command
    table["args"] = args
    return table


def inject_mcp_config(target: AgentTarget) -> Path | None:
    """Register Protean as an MCP server in the runtime's user config.

    Codex (TOML): set ``mcp_servers.protean`` via tomlkit so the user's
    other tables, comments, and formatting are preserved verbatim.

    Claude Code (JSON): parse, set ``mcpServers.protean``, write back.
    Other keys are preserved verbatim.

    Returns the config path written, or ``None`` when the target has no
    MCP config convention.
    """
    path = target.mcp_config_file
    fmt = target.mcp_config_format
    if path is None or fmt is None:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "toml":
        if path.exists():
            doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        else:
            doc = tomlkit.document()
        servers = doc.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = tomlkit.table()
            doc["mcp_servers"] = servers
        servers[_MCP_SERVER_NAME] = _mcp_toml_table()
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return path

    if fmt == "json":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {}
        servers = data.setdefault("mcpServers", {})
        command, args = _mcp_command_args()
        servers[_MCP_SERVER_NAME] = {
            "type": "stdio",
            "command": command,
            "args": args,
            "env": {},
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path

    raise ValueError(f"Unsupported MCP config format: {fmt}")


def remove_mcp_config(target: AgentTarget) -> Path | None:
    """Remove the Protean MCP entry from the runtime's user config.

    Returns the path touched, or ``None`` when there was nothing to do.
    """
    path = target.mcp_config_file
    fmt = target.mcp_config_format
    if path is None or fmt is None or not path.exists():
        return None

    if fmt == "toml":
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        servers = doc.get("mcp_servers")
        if not isinstance(servers, dict) or _MCP_SERVER_NAME not in servers:
            return None
        del servers[_MCP_SERVER_NAME]
        # Drop the parent table if Protean was the only entry; otherwise
        # leave the user's other mcp_servers alone.
        if len(servers) == 0:
            del doc["mcp_servers"]
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return path

    if fmt == "json":
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers")
        if not isinstance(servers, dict) or _MCP_SERVER_NAME not in servers:
            return None
        del servers[_MCP_SERVER_NAME]
        if not servers:
            del data["mcpServers"]
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path

    raise ValueError(f"Unsupported MCP config format: {fmt}")


def copy_skill_dirs(
    pairs: "Iterable[tuple[Skill, Path | None]]",
    dest_root: Path,
) -> list[Path]:
    dest_root.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for skill, src in pairs:
        if src is None or not src.exists():
            continue
        dst = dest_root / skill.name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        copied.append(dst)
    return copied


def load_exportable_skill_pairs(skills_dir: Path) -> list[tuple["Skill", Path | None]]:
    registry = SkillRegistry(skills_dir)
    registry.load_all()
    pairs: list[tuple["Skill", Path | None]] = []
    for skill in sorted(registry.list_skills(), key=lambda item: item.name):
        if skill.name == AGENT_PROTEAN_SKILL_NAME:
            continue
        pairs.append((skill, registry.get_path(skill.name)))
    return pairs


def setup_agent(
    target: AgentTarget,
    *,
    extra_skill_pairs: "Iterable[tuple[Skill, Path | None]]" = (),
) -> AgentSetupResult:
    target.skills_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_skill = build_and_evolve_skills_with_protean_skill(
        source_default=target.source,
    )
    bootstrap_path = install_bootstrap_skill(target, bootstrap_skill)
    copied_skills = copy_skill_dirs(
        extra_skill_pairs,
        target.skills_dir,
    )
    instructions_path = inject_instructions(target, bootstrap_skill)
    mcp_config_path = inject_mcp_config(target)
    return AgentSetupResult(
        target=target,
        bootstrap_path=bootstrap_path,
        copied_skills=copied_skills,
        instructions_path=instructions_path,
        mcp_config_path=mcp_config_path,
    )


def remove_instructions(target: AgentTarget) -> tuple[Path | None, bool]:
    """Remove the Protean-managed block from the agent's instructions file.

    Returns ``(path, file_deleted)``. ``path`` is the file we touched, or
    ``None`` if there was nothing to do (no target file, file missing, or
    no managed block present). ``file_deleted`` is True when the file
    contained only the managed block and was removed entirely.
    """
    path = target.instructions_file
    if path is None or not path.exists():
        return None, False

    if not remove_sentinel_block(
        path,
        begin=_INSTRUCTIONS_BLOCK_BEGIN,
        end=_INSTRUCTIONS_BLOCK_END,
    ):
        return None, False

    if path.read_text(encoding="utf-8") == "":
        path.unlink()
        return path, True
    return path, False


def uninstall_agent(
    target: AgentTarget,
    *,
    managed_skill_names: "Iterable[str]" = (),
) -> AgentUninstallResult:
    """Reverse ``setup_agent`` for a single runtime.

    Removes the bootstrap skill folder, every folder under
    ``target.skills_dir`` whose name appears in ``managed_skill_names``
    (i.e. skills Protean copied in), and the sentinel-delimited block
    from the instructions file. All operations are idempotent and safe
    to call on a partially-installed target.
    """
    removed_bootstrap: Path | None = None
    bootstrap_dir = target.skills_dir / AGENT_PROTEAN_SKILL_NAME
    if bootstrap_dir.exists():
        shutil.rmtree(bootstrap_dir)
        removed_bootstrap = bootstrap_dir

    removed_skills: list[Path] = []
    for name in managed_skill_names:
        if name == AGENT_PROTEAN_SKILL_NAME:
            continue
        skill_dir = target.skills_dir / name
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
            removed_skills.append(skill_dir)

    instructions_path, file_deleted = remove_instructions(target)
    mcp_config_path = remove_mcp_config(target)

    return AgentUninstallResult(
        target=target,
        removed_bootstrap=removed_bootstrap,
        removed_skills=removed_skills,
        instructions_path=instructions_path,
        instructions_file_deleted=file_deleted,
        mcp_config_path=mcp_config_path,
    )


def managed_skill_names(skills_dir: Path) -> list[str]:
    """Return the names of skills Protean would copy into an agent runtime.

    Used by uninstall to decide which folders under the agent's skills_dir
    are Protean-managed and safe to remove.
    """
    return [skill.name for skill, _ in load_exportable_skill_pairs(skills_dir)]


# Order matters: probe more specific targets first so a host with both
# Codex and Claude installed gets both synced.
_KNOWN_AGENT_KINDS: tuple[str, ...] = ("codex", "claude_code")


def installed_agent_targets() -> list[AgentTarget]:
    """Return agent targets that have been ``agents setup`` previously.

    Detection marker is the bootstrap skill — if a target's skills dir
    contains ``<AGENT_PROTEAN_SKILL_NAME>/SKILL.md``, that runtime has
    Protean installed and is a sync destination.
    """
    installed: list[AgentTarget] = []
    for kind in _KNOWN_AGENT_KINDS:
        try:
            target = resolve_agent_target(kind)
        except ValueError:
            continue
        marker = target.skills_dir / AGENT_PROTEAN_SKILL_NAME / "SKILL.md"
        if marker.exists():
            installed.append(target)
    return installed


def sync_installed_agents(
    skills_dir: Path,
) -> list[AgentSetupResult]:
    """Re-run ``setup_agent`` for every installed runtime.

    Called after any pipeline that writes to ``skills_dir`` (generate,
    daemon hotkey, trajectories evolve, skills run --refine) so the
    agent runtimes see the new/updated skill without the user remembering
    to re-run ``agents setup``. No-op when no target is installed.
    """
    targets = installed_agent_targets()
    if not targets:
        return []
    extra_pairs = load_exportable_skill_pairs(skills_dir)
    return [
        setup_agent(
            target,
            extra_skill_pairs=extra_pairs,
        )
        for target in targets
    ]

"""Install Protean agent skills into local agent runtimes."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from protean.skills.bootstrap import (
    AGENT_PROTEAN_SKILL_NAME,
    build_and_evolve_skills_with_protean_skill,
)
from protean.skills.registry import SkillRegistry
from protean.skills.renderer import render_skill

if TYPE_CHECKING:
    from collections.abc import Iterable

    from protean.skills.schema import Skill


@dataclass(frozen=True)
class AgentTarget:
    display_name: str
    source: str
    skills_dir: Path
    # User-level instruction file the runtime auto-loads on session start
    # (Codex: AGENTS.md, Claude Code: CLAUDE.md). None = runtime has no such
    # convention, so instruction injection is skipped.
    instructions_file: Path | None = None


@dataclass(frozen=True)
class AgentSetupResult:
    target: AgentTarget
    bootstrap_path: Path
    copied_skills: list[Path]
    instructions_path: Path | None


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


def resolve_agent_target(kind: str) -> AgentTarget:
    normalized = kind.strip().lower().replace("-", "_")
    if normalized == "codex":
        home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        return AgentTarget(
            display_name="Codex",
            source="codex",
            skills_dir=home / "skills",
            instructions_file=home / "AGENTS.md",
        )
    if normalized in {"claude", "claude_code"}:
        home = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude"))).expanduser()
        return AgentTarget(
            display_name="Claude Code",
            source="claude_code",
            skills_dir=home / "skills",
            instructions_file=home / "CLAUDE.md",
        )
    raise ValueError(f"Unsupported agent target: {kind}")


def install_bootstrap_skill(
    target: AgentTarget,
    skill: "Skill",
) -> Path:
    return render_skill(skill, target.skills_dir / skill.name)


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
"""


def inject_instructions(target: AgentTarget, skill: "Skill") -> Path | None:
    """Insert or refresh the Protean block in the agent's instruction file.

    Strategy:

    - If the file already contains the sentinel markers, replace everything
      between them (the user keeps their surrounding content).
    - Otherwise append a new block to the end (or create the file).

    Returns the path written, or ``None`` when the target has no
    instructions-file convention.
    """
    path = target.instructions_file
    if path is None:
        return None

    body = _INSTRUCTIONS_BLOCK_TEMPLATE.format(skill_name=skill.name)
    block = f"{_INSTRUCTIONS_BLOCK_BEGIN}\n{body}\n{_INSTRUCTIONS_BLOCK_END}"

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    begin_idx = existing.find(_INSTRUCTIONS_BLOCK_BEGIN)
    end_idx = existing.find(_INSTRUCTIONS_BLOCK_END)
    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        end_idx += len(_INSTRUCTIONS_BLOCK_END)
        updated = existing[:begin_idx] + block + existing[end_idx:]
    elif existing.strip():
        updated = f"{existing.rstrip()}\n\n{block}\n"
    else:
        updated = f"{block}\n"

    if updated != existing:
        path.write_text(updated, encoding="utf-8")
    return path


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
    protean_root: Path,
    extra_skill_pairs: "Iterable[tuple[Skill, Path | None]]" = (),
) -> AgentSetupResult:
    target.skills_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_skill = build_and_evolve_skills_with_protean_skill(
        source_default=target.source,
        protean_root=protean_root,
    )
    bootstrap_path = install_bootstrap_skill(target, bootstrap_skill)
    copied_skills = copy_skill_dirs(
        extra_skill_pairs,
        target.skills_dir,
    )
    instructions_path = inject_instructions(target, bootstrap_skill)
    return AgentSetupResult(
        target=target,
        bootstrap_path=bootstrap_path,
        copied_skills=copied_skills,
        instructions_path=instructions_path,
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

    existing = path.read_text(encoding="utf-8")
    begin_idx = existing.find(_INSTRUCTIONS_BLOCK_BEGIN)
    end_idx = existing.find(_INSTRUCTIONS_BLOCK_END)
    if begin_idx == -1 or end_idx == -1 or end_idx <= begin_idx:
        return None, False

    end_idx += len(_INSTRUCTIONS_BLOCK_END)
    updated = (existing[:begin_idx] + existing[end_idx:]).strip()

    if not updated:
        path.unlink()
        return path, True

    # Preserve a trailing newline so the file remains POSIX-clean.
    path.write_text(updated + "\n", encoding="utf-8")
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

    return AgentUninstallResult(
        target=target,
        removed_bootstrap=removed_bootstrap,
        removed_skills=removed_skills,
        instructions_path=instructions_path,
        instructions_file_deleted=file_deleted,
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
    *,
    protean_root: Path,
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
            protean_root=protean_root,
            extra_skill_pairs=extra_pairs,
        )
        for target in targets
    ]

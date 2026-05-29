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


@dataclass(frozen=True)
class AgentSetupResult:
    target: AgentTarget
    bootstrap_path: Path
    copied_skills: list[Path]


def resolve_agent_target(kind: str) -> AgentTarget:
    normalized = kind.strip().lower().replace("-", "_")
    if normalized == "codex":
        home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        return AgentTarget(
            display_name="Codex",
            source="codex",
            skills_dir=home / "skills",
        )
    if normalized in {"claude", "claude_code"}:
        home = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude"))).expanduser()
        return AgentTarget(
            display_name="Claude Code",
            source="claude_code",
            skills_dir=home / "skills",
        )
    raise ValueError(f"Unsupported agent target: {kind}")


def install_bootstrap_skill(
    target: AgentTarget,
    *,
    protean_root: Path,
) -> Path:
    skill = build_and_evolve_skills_with_protean_skill(
        source_default=target.source,
        protean_root=protean_root,
    )
    return render_skill(skill, target.skills_dir / skill.name)


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
    bootstrap_path = install_bootstrap_skill(
        target,
        protean_root=protean_root,
    )
    copied_skills = copy_skill_dirs(
        extra_skill_pairs,
        target.skills_dir,
    )
    return AgentSetupResult(
        target=target,
        bootstrap_path=bootstrap_path,
        copied_skills=copied_skills,
    )


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

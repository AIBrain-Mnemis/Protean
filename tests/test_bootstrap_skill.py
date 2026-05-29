from __future__ import annotations

from pathlib import Path

from protean.skills.bootstrap import (
    AGENT_PROTEAN_SKILL_NAME,
    build_and_evolve_skills_with_protean_skill,
)
from protean.skills.registry import load_skill_from_file
from protean.skills.renderer import render_skill, render_skill_markdown


def test_bootstrap_skill_renders_with_protean_renderer(tmp_path: Path):
    skill = build_and_evolve_skills_with_protean_skill()
    rendered = render_skill_markdown(skill)

    assert skill.name == AGENT_PROTEAN_SKILL_NAME
    assert "build, run, validate, import, hand-edit, refine, and evolve" in skill.description
    assert "agents setup" in rendered
    assert "Use Protean as the local capability factory" in rendered
    assert "Workflow choice" in rendered
    assert "Codex" in rendered
    assert "Claude Code" in rendered
    assert "Recorded screen demonstration" in rendered
    assert "Realtime voice or screen-share session" in rendered
    assert "Zero-shot task prompt" in rendered
    assert "Hand-edit a SKILL.md" in rendered
    assert "Import another skill library" in rendered
    assert "Agent self-refinement during execution" in rendered
    assert "Current-session trajectory evolution" in rendered
    assert "trajectories mark start" in rendered
    assert "trajectories evolve" in rendered
    assert '--source "{{source}}"' in rendered
    assert "local runtime session transcript" in rendered
    assert "simple ReAct stream" in rendered
    assert "invite attendees as irreversible" in rendered
    assert "--overlay" not in rendered
    assert "overlay" not in rendered.lower()

    md_path = render_skill(skill, tmp_path / skill.name)
    parsed = load_skill_from_file(md_path)
    assert parsed.name == skill.name
    assert [step.name for step in parsed.steps] == [step.name for step in skill.steps]

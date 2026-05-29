from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from protean.agent_setup import (
    AgentTarget,
    copy_skill_dirs,
    install_bootstrap_skill,
)
from protean.cli import main
from protean.skills.bootstrap import AGENT_PROTEAN_SKILL_NAME
from protean.skills.registry import load_skill_from_file
from protean.skills.schema import Skill


def test_install_bootstrap_skill_adapts_source_and_repo_root(tmp_path: Path):
    target = AgentTarget(
        display_name="Claude Code",
        source="claude_code",
        skills_dir=tmp_path / "agent-skills",
    )
    md_path = install_bootstrap_skill(
        target,
        protean_root=tmp_path / "Protean Repo",
    )

    text = md_path.read_text(encoding="utf-8")
    parsed = load_skill_from_file(md_path)
    source_param = next(p for p in parsed.parameters if p.name == "source")

    assert source_param.default == "claude_code"
    assert "cd " in text
    assert "Protean Repo" in text
    assert "--source \"{{source}}\"" in text
    assert "trajectories evolve" in text


def test_copy_skill_dirs_uses_skill_name_and_overwrites_existing(tmp_path: Path):
    skill = Skill(name="demo-skill", description="Demo skill")
    src = tmp_path / "source-dir"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo\n---\n",
        encoding="utf-8",
    )

    dest = tmp_path / "dest"
    copied = copy_skill_dirs([(skill, src)], dest)
    assert copied == [dest / "demo-skill"]
    assert (dest / "demo-skill" / "SKILL.md").exists()

    (dest / "demo-skill" / "sentinel.txt").write_text("replace", encoding="utf-8")
    copied_again = copy_skill_dirs([(skill, src)], dest)
    assert copied_again == [dest / "demo-skill"]
    assert not (dest / "demo-skill" / "sentinel.txt").exists()


def test_agents_setup_copies_skill_library_by_default(tmp_path: Path, monkeypatch):
    skills_root = tmp_path / "protean-skills"
    source_skill = skills_root / "demo-skill"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo\n---\n\nDemo body\n",
        encoding="utf-8",
    )
    generic_bootstrap = skills_root / AGENT_PROTEAN_SKILL_NAME
    generic_bootstrap.mkdir()
    (generic_bootstrap / "SKILL.md").write_text(
        f"---\nname: {AGENT_PROTEAN_SKILL_NAME}\ndescription: Generic\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROTEAN_SKILLS_DIR", str(skills_root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "setup", "codex"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "codex-home" / "skills" / "demo-skill" / "SKILL.md").exists()
    bootstrap_text = (
        tmp_path / "codex-home" / "skills" / AGENT_PROTEAN_SKILL_NAME / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "source, such as codex or claude_code. (default: `codex`)" in bootstrap_text
    assert "description: Generic" not in bootstrap_text


def test_agents_setup_adapts_claude_code_target(tmp_path: Path, monkeypatch):
    skills_root = tmp_path / "protean-skills"
    source_skill = skills_root / "demo-skill"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo\n---\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("PROTEAN_SKILLS_DIR", str(skills_root))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "setup", "claude_code"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "claude-home" / "skills" / "demo-skill" / "SKILL.md").exists()
    bootstrap_text = (
        tmp_path / "claude-home" / "skills" / AGENT_PROTEAN_SKILL_NAME / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "source, such as codex or claude_code. (default: `claude_code`)" in bootstrap_text

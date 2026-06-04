from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from protean.agent_setup import (
    AgentTarget,
    copy_skill_dirs,
    inject_instructions,
    install_bootstrap_skill,
    remove_instructions,
    uninstall_agent,
)
from protean.cli import main
from protean.skills.bootstrap import (
    AGENT_PROTEAN_SKILL_NAME,
    build_and_evolve_skills_with_protean_skill,
)
from protean.skills.registry import load_skill_from_file
from protean.skills.schema import Skill


def _bootstrap_skill(protean_root: Path | None = None, source: str = "codex") -> Skill:
    return build_and_evolve_skills_with_protean_skill(
        source_default=source,
        protean_root=protean_root,
    )


def test_install_bootstrap_skill_adapts_source_and_repo_root(tmp_path: Path):
    target = AgentTarget(
        display_name="Claude Code",
        source="claude_code",
        skills_dir=tmp_path / "agent-skills",
    )
    skill = _bootstrap_skill(protean_root=tmp_path / "Protean Repo", source="claude_code")
    md_path = install_bootstrap_skill(target, skill)

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


def _make_target(tmp_path: Path, *, instructions_file: Path | None) -> AgentTarget:
    return AgentTarget(
        display_name="Codex",
        source="codex",
        skills_dir=tmp_path / "skills",
        instructions_file=instructions_file,
    )


def test_inject_instructions_creates_file_when_missing(tmp_path: Path):
    instr = tmp_path / "AGENTS.md"
    target = _make_target(tmp_path, instructions_file=instr)
    skill = _bootstrap_skill()

    written = inject_instructions(target, skill)

    assert written == instr
    text = instr.read_text(encoding="utf-8")
    assert "<!-- protean:begin -->" in text
    assert "<!-- protean:end -->" in text
    # The block names the skill and tells the model to load it at session start.
    assert AGENT_PROTEAN_SKILL_NAME in text
    assert "At the start of each new chat" in text
    assert "source of truth" in text
    # Runtime already knows where its skills live; the absolute path is not repeated.
    assert str(target.skills_dir) not in text


def test_inject_instructions_appends_when_file_has_unrelated_content(tmp_path: Path):
    instr = tmp_path / "AGENTS.md"
    instr.write_text("# My rules\n\nDo good work.\n", encoding="utf-8")
    target = _make_target(tmp_path, instructions_file=instr)

    inject_instructions(target, _bootstrap_skill())

    text = instr.read_text(encoding="utf-8")
    assert text.startswith("# My rules\n\nDo good work.")
    assert "<!-- protean:begin -->" in text


def test_inject_instructions_replaces_existing_block_in_place(tmp_path: Path):
    instr = tmp_path / "AGENTS.md"
    instr.write_text(
        "# My rules\n\nDo good work.\n\n"
        "<!-- protean:begin -->\n"
        "STALE CONTENT\n"
        "<!-- protean:end -->\n\n"
        "## After block\n\nMore rules.\n",
        encoding="utf-8",
    )
    target = _make_target(tmp_path, instructions_file=instr)

    inject_instructions(target, _bootstrap_skill())

    text = instr.read_text(encoding="utf-8")
    assert "STALE CONTENT" not in text
    assert "## After block" in text  # user content after block preserved
    assert "# My rules" in text  # user content before block preserved
    assert text.count("<!-- protean:begin -->") == 1
    assert AGENT_PROTEAN_SKILL_NAME in text


def test_inject_instructions_is_idempotent(tmp_path: Path):
    instr = tmp_path / "AGENTS.md"
    target = _make_target(tmp_path, instructions_file=instr)
    skill = _bootstrap_skill()

    inject_instructions(target, skill)
    first = instr.read_text(encoding="utf-8")
    mtime = instr.stat().st_mtime_ns

    inject_instructions(target, skill)
    assert instr.read_text(encoding="utf-8") == first
    # No write when content is unchanged.
    assert instr.stat().st_mtime_ns == mtime


def test_inject_instructions_skips_when_target_has_no_file():
    target = AgentTarget(
        display_name="Generic",
        source="generic",
        skills_dir=Path("/tmp/skills-unused"),
        instructions_file=None,
    )
    assert inject_instructions(target, _bootstrap_skill()) is None


def test_agents_setup_writes_instructions_file(tmp_path: Path, monkeypatch):
    skills_root = tmp_path / "protean-skills"
    skills_root.mkdir()
    monkeypatch.setenv("PROTEAN_SKILLS_DIR", str(skills_root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "setup", "codex"])

    assert result.exit_code == 0, result.output
    agents_md = tmp_path / "codex-home" / "AGENTS.md"
    assert agents_md.exists()
    text = agents_md.read_text(encoding="utf-8")
    assert "<!-- protean:begin -->" in text
    assert AGENT_PROTEAN_SKILL_NAME in text
    assert "At the start of each new chat" in text
    assert "Instructions:" in result.output


def test_remove_instructions_deletes_file_when_only_managed_block(tmp_path: Path):
    instr = tmp_path / "AGENTS.md"
    target = _make_target(tmp_path, instructions_file=instr)
    inject_instructions(target, _bootstrap_skill())
    assert instr.exists()

    path, deleted = remove_instructions(target)

    assert path == instr
    assert deleted is True
    assert not instr.exists()


def test_remove_instructions_preserves_user_content(tmp_path: Path):
    instr = tmp_path / "AGENTS.md"
    instr.write_text("# My rules\n\nDo good work.\n", encoding="utf-8")
    target = _make_target(tmp_path, instructions_file=instr)
    inject_instructions(target, _bootstrap_skill())

    path, deleted = remove_instructions(target)

    assert path == instr
    assert deleted is False
    text = instr.read_text(encoding="utf-8")
    assert "# My rules" in text
    assert "Do good work." in text
    assert "<!-- protean:begin -->" not in text


def test_remove_instructions_is_noop_when_no_block_present(tmp_path: Path):
    instr = tmp_path / "AGENTS.md"
    instr.write_text("# My rules\n", encoding="utf-8")
    target = _make_target(tmp_path, instructions_file=instr)

    path, deleted = remove_instructions(target)

    assert path is None
    assert deleted is False
    assert instr.read_text(encoding="utf-8") == "# My rules\n"


def test_uninstall_agent_removes_bootstrap_and_managed_skills(tmp_path: Path):
    target = _make_target(tmp_path, instructions_file=tmp_path / "AGENTS.md")
    # Set up: bootstrap, two managed skills, one user-authored skill.
    install_bootstrap_skill(target, _bootstrap_skill())
    inject_instructions(target, _bootstrap_skill())
    for name in ("managed-a", "managed-b", "user-authored"):
        (target.skills_dir / name).mkdir(parents=True, exist_ok=True)
        (target.skills_dir / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: x\n---\n", encoding="utf-8",
        )

    result = uninstall_agent(target, managed_skill_names=["managed-a", "managed-b"])

    assert result.removed_bootstrap == target.skills_dir / AGENT_PROTEAN_SKILL_NAME
    assert sorted(p.name for p in result.removed_skills) == ["managed-a", "managed-b"]
    assert result.instructions_file_deleted is True
    # User-authored skill is untouched.
    assert (target.skills_dir / "user-authored").exists()
    # Managed skills + bootstrap are gone.
    assert not (target.skills_dir / AGENT_PROTEAN_SKILL_NAME).exists()
    assert not (target.skills_dir / "managed-a").exists()
    assert not (target.skills_dir / "managed-b").exists()


def test_uninstall_agent_is_idempotent(tmp_path: Path):
    target = _make_target(tmp_path, instructions_file=tmp_path / "AGENTS.md")
    install_bootstrap_skill(target, _bootstrap_skill())
    inject_instructions(target, _bootstrap_skill())

    uninstall_agent(target, managed_skill_names=[])
    # Second call must not raise and must report nothing left to remove.
    result = uninstall_agent(target, managed_skill_names=[])
    assert result.removed_bootstrap is None
    assert result.removed_skills == []
    assert result.instructions_path is None
    assert result.instructions_file_deleted is False


def test_agents_uninstall_cli_removes_setup(tmp_path: Path, monkeypatch):
    skills_root = tmp_path / "protean-skills"
    source_skill = skills_root / "demo-skill"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo\n---\n", encoding="utf-8",
    )
    monkeypatch.setenv("PROTEAN_SKILLS_DIR", str(skills_root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    runner = CliRunner()

    setup_result = runner.invoke(main, ["agents", "setup", "codex"])
    assert setup_result.exit_code == 0, setup_result.output
    codex_skills = tmp_path / "codex-home" / "skills"
    assert (codex_skills / AGENT_PROTEAN_SKILL_NAME / "SKILL.md").exists()
    assert (codex_skills / "demo-skill" / "SKILL.md").exists()
    assert (tmp_path / "codex-home" / "AGENTS.md").exists()

    uninstall_result = runner.invoke(main, ["agents", "uninstall", "codex"])
    assert uninstall_result.exit_code == 0, uninstall_result.output
    assert not (codex_skills / AGENT_PROTEAN_SKILL_NAME).exists()
    assert not (codex_skills / "demo-skill").exists()
    assert not (tmp_path / "codex-home" / "AGENTS.md").exists()


def test_agents_uninstall_all_only_targets_installed(tmp_path: Path, monkeypatch):
    skills_root = tmp_path / "protean-skills"
    skills_root.mkdir()
    monkeypatch.setenv("PROTEAN_SKILLS_DIR", str(skills_root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    runner = CliRunner()

    # Only set up codex; claude_code remains uninstalled.
    runner.invoke(main, ["agents", "setup", "codex"])
    assert (tmp_path / "codex-home" / "skills" / AGENT_PROTEAN_SKILL_NAME).exists()
    assert not (tmp_path / "claude-home").exists()

    result = runner.invoke(main, ["agents", "uninstall", "all"])
    assert result.exit_code == 0, result.output
    assert "Codex" in result.output
    assert "Claude Code" not in result.output
    assert not (tmp_path / "codex-home" / "skills" / AGENT_PROTEAN_SKILL_NAME).exists()

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from protean.agent_setup import (
    AgentTarget,
    copy_skill_dirs,
    inject_instructions,
    inject_mcp_config,
    install_bootstrap_skill,
    remove_instructions,
    remove_mcp_config,
    resolve_agent_target,
    uninstall_agent,
)
from protean.cli import main
from protean.skills.bootstrap import (
    AGENT_PROTEAN_SKILL_NAME,
    build_and_evolve_skills_with_protean_skill,
)
from protean.skills.registry import load_skill_from_file
from protean.skills.schema import Skill


def _bootstrap_skill(source: str = "codex") -> Skill:
    return build_and_evolve_skills_with_protean_skill(source_default=source)


def test_install_bootstrap_skill_adapts_source(tmp_path: Path):
    target = AgentTarget(
        display_name="Claude Code",
        source="claude_code",
        skills_dir=tmp_path / "agent-skills",
    )
    skill = _bootstrap_skill(source="claude_code")
    md_path = install_bootstrap_skill(target, skill)

    text = md_path.read_text(encoding="utf-8")
    parsed = load_skill_from_file(md_path)
    source_param = next(p for p in parsed.parameters if p.name == "source")

    assert source_param.default == "claude_code"
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


# ---------- MCP config injection ---------------------------------------------


def _codex_target(tmp_path: Path) -> AgentTarget:
    return AgentTarget(
        display_name="Codex",
        source="codex",
        skills_dir=tmp_path / "skills",
        instructions_file=tmp_path / "AGENTS.md",
        mcp_config_file=tmp_path / "config.toml",
        mcp_config_format="toml",
    )


def _claude_target(tmp_path: Path) -> AgentTarget:
    return AgentTarget(
        display_name="Claude Code",
        source="claude_code",
        skills_dir=tmp_path / "claude" / "skills",
        instructions_file=tmp_path / "claude" / "CLAUDE.md",
        mcp_config_file=tmp_path / "claude.json",
        mcp_config_format="json",
    )


def test_inject_mcp_config_toml_creates_file(tmp_path: Path):
    import tomllib
    target = _codex_target(tmp_path)
    path = inject_mcp_config(target)
    assert path == target.mcp_config_file
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    assert doc["mcp_servers"]["protean"] == {
        "command": "protean",
        "args": ["mcp"],
    }


def test_inject_mcp_config_toml_replaces_only_protean_entry(tmp_path: Path):
    import tomllib
    target = _codex_target(tmp_path)
    target.mcp_config_file.write_text(
        'model = "gpt-5"\n'
        '\n'
        '[mcp_servers.protean]\n'
        'command = "stale"\n'
        '\n'
        '[other.section]\n'
        'key = 1\n',
        encoding="utf-8",
    )

    inject_mcp_config(target)

    text = target.mcp_config_file.read_text(encoding="utf-8")
    doc = tomllib.loads(text)
    assert doc["mcp_servers"]["protean"]["command"] == "protean"
    assert doc["model"] == "gpt-5"  # user content preserved
    assert doc["other"]["section"]["key"] == 1  # sibling table preserved


def test_inject_mcp_config_toml_preserves_sibling_mcp_servers(tmp_path: Path):
    """Regression test: a sibling [mcp_servers.<name>] table must survive.

    The previous sentinel-based implementation wrapped a literal text block
    and could absorb any later-added section into its range; codex adds
    [mcp_servers.node_repl] on startup, which was being deleted by uninstall.
    """
    import tomllib
    target = _codex_target(tmp_path)
    target.mcp_config_file.write_text(
        '[mcp_servers.node_repl]\n'
        'command = "/Applications/Codex.app/Contents/Resources/node_repl"\n'
        'args = []\n'
        '\n'
        '[mcp_servers.node_repl.env]\n'
        'NODE_REPL_FOO = "bar"\n',
        encoding="utf-8",
    )

    inject_mcp_config(target)

    doc = tomllib.loads(target.mcp_config_file.read_text(encoding="utf-8"))
    assert doc["mcp_servers"]["protean"]["command"] == "protean"
    assert doc["mcp_servers"]["node_repl"]["command"].endswith("node_repl")
    assert doc["mcp_servers"]["node_repl"]["env"]["NODE_REPL_FOO"] == "bar"


def test_inject_mcp_config_toml_is_idempotent(tmp_path: Path):
    target = _codex_target(tmp_path)
    inject_mcp_config(target)
    first = target.mcp_config_file.read_text(encoding="utf-8")
    inject_mcp_config(target)
    assert target.mcp_config_file.read_text(encoding="utf-8") == first


def test_inject_mcp_config_json_creates_file(tmp_path: Path):
    target = _claude_target(tmp_path)
    path = inject_mcp_config(target)
    assert path == target.mcp_config_file
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["protean"] == {
        "type": "stdio",
        "command": "protean",
        "args": ["mcp"],
        "env": {},
    }


def test_inject_mcp_config_json_preserves_other_keys(tmp_path: Path):
    import json
    target = _claude_target(tmp_path)
    target.mcp_config_file.write_text(
        json.dumps({"numStartups": 7, "mcpServers": {"other": {"command": "x"}}}),
        encoding="utf-8",
    )

    inject_mcp_config(target)

    data = json.loads(target.mcp_config_file.read_text(encoding="utf-8"))
    assert data["numStartups"] == 7
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["protean"]["command"] == "protean"


def test_remove_mcp_config_toml_preserves_surrounding_content(tmp_path: Path):
    import tomllib
    target = _codex_target(tmp_path)
    target.mcp_config_file.write_text(
        '[mcp_servers.node_repl]\n'
        'command = "/Applications/Codex.app/Contents/Resources/node_repl"\n'
        '\n'
        '[other.section]\n'
        'k = 1\n',
        encoding="utf-8",
    )
    inject_mcp_config(target)

    path = remove_mcp_config(target)
    assert path == target.mcp_config_file

    doc = tomllib.loads(target.mcp_config_file.read_text(encoding="utf-8"))
    assert "protean" not in doc.get("mcp_servers", {})
    assert doc["mcp_servers"]["node_repl"]["command"].endswith("node_repl")
    assert doc["other"]["section"]["k"] == 1


def test_remove_mcp_config_json_drops_only_protean_entry(tmp_path: Path):
    import json
    target = _claude_target(tmp_path)
    target.mcp_config_file.write_text(
        json.dumps({"numStartups": 7, "mcpServers": {"other": {"command": "x"}}}),
        encoding="utf-8",
    )
    inject_mcp_config(target)

    remove_mcp_config(target)

    data = json.loads(target.mcp_config_file.read_text(encoding="utf-8"))
    assert data["numStartups"] == 7
    assert data["mcpServers"] == {"other": {"command": "x"}}


def test_remove_mcp_config_is_noop_when_no_entry(tmp_path: Path):
    target = _codex_target(tmp_path)
    target.mcp_config_file.write_text("model = \"gpt-5\"\n", encoding="utf-8")

    assert remove_mcp_config(target) is None
    assert target.mcp_config_file.read_text(encoding="utf-8") == "model = \"gpt-5\"\n"


def test_claude_target_mcp_path_follows_claude_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "custom-claude"))
    target = resolve_agent_target("claude_code")
    assert target.mcp_config_file == tmp_path / "custom-claude.json"


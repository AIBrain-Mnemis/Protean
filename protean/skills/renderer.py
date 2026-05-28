"""Skill renderer — render a Skill object to SKILL.md + scripts/ directory.

Converts the internal Pydantic Skill model into the Agent Skills standard format:

  my-skill/
    SKILL.md         ← YAML frontmatter + Markdown body
    scripts/         ← executable scripts (optional)
      run.sh
      process.py
"""

from __future__ import annotations

import stat
from pathlib import Path

import yaml

from protean.skills.schema import Skill


def render_skill_markdown(skill: Skill) -> str:
    """Render a Skill to SKILL.md text without writing files."""
    # ── Build YAML frontmatter (Agent Skills spec compliant) ──
    frontmatter: dict = {
        "name": skill.name,
        "description": skill.description,
    }

    # Pack protean-specific metadata
    protean_meta: dict = {}
    if skill.requires.bins or skill.requires.env:
        protean_meta["requires"] = skill.requires.model_dump(exclude_defaults=True)
    if skill.install:
        protean_meta["install"] = skill.install
    if skill.source:
        protean_meta["source"] = skill.source
    if skill.related_skills:
        protean_meta["related_skills"] = skill.related_skills
    if skill.metadata:
        protean_meta.update(skill.metadata)

    meta: dict = {}
    if protean_meta:
        meta["protean"] = protean_meta
    if skill.tags:
        meta["tags"] = skill.tags
    if meta:
        frontmatter["metadata"] = meta

    # ── Build Markdown body ──
    lines: list[str] = []

    # Title + description
    lines.append(f"# {_title_case(skill.name)}")
    lines.append("")
    lines.append(f"> {skill.description}")
    lines.append("")

    # When to use / not use
    if skill.when_to_use:
        lines.append("## When to Use")
        lines.append("")
        for trigger in skill.when_to_use:
            lines.append(f"- {trigger}")
        lines.append("")

    if skill.when_not_to_use:
        lines.append("## When NOT to Use")
        lines.append("")
        for anti in skill.when_not_to_use:
            lines.append(f"- {anti}")
        lines.append("")

    # Inputs
    if skill.parameters:
        lines.append("## Inputs")
        lines.append("")
        for p in skill.parameters:
            req = "required" if p.required else "optional"
            line = f"- **{p.name}** ({req}): {p.description}"
            if p.default:
                line += f" (default: `{p.default}`)"
            if p.constraints:
                line += f" — {p.constraints}"
            lines.append(line)
        lines.append("")

    # Goal
    if skill.goal:
        lines.append("## Goal")
        lines.append("")
        lines.append(skill.goal)
        lines.append("")

    # Steps
    if skill.steps:
        lines.append("## Steps")
        lines.append("")
        for i, step in enumerate(skill.steps, 1):
            lines.append(f"### {i}. {_title_case(step.name)}")
            lines.append("")

            if step.target_app:
                lines.append(f"**App:** {step.target_app}")
                lines.append("")

            if step.tool:
                lines.append(f"**Tool:** `{step.tool}`")
                lines.append("")

            lines.append(step.action)
            lines.append("")

            if step.figures:
                for fig in step.figures:
                    lines.append(f"![{fig.caption}](figs/{fig.ref})")
                lines.append("")

            if step.verify_condition:
                vc = step.verify_condition
                if vc.description:
                    lines.append(f"**Verify:** {vc.description}")
                    lines.append("")
                lines.append(f"**Verify Condition:** strategy={vc.strategy}")
                if vc.ax_role:
                    lines.append(f"  ax_role={vc.ax_role}")
                if vc.ax_title:
                    lines.append(f"  ax_title={vc.ax_title}")
                if vc.expected_text:
                    lines.append(f"  expected_text={vc.expected_text}")
                if vc.description:
                    lines.append(f"  description={vc.description}")
                lines.append("")

            if not step.idempotent:
                lines.append("**Idempotent:** no")
                lines.append("")

            if step.branches:
                for br in step.branches:
                    vc = br.condition
                    cond_parts = [f"strategy={vc.strategy}"]
                    if vc.description:
                        cond_parts.append(f"description={vc.description}")
                    if vc.ax_role:
                        cond_parts.append(f"ax_role={vc.ax_role}")
                    if vc.ax_title:
                        cond_parts.append(f"ax_title={vc.ax_title}")
                    if vc.expected_text:
                        cond_parts.append(
                            f"expected_text={vc.expected_text}"
                        )
                    lines.append(
                        f"**Branch:** if {', '.join(cond_parts)} "
                        f"→ `{br.next_step}`"
                    )
                lines.append("")

    # Success criteria
    if skill.success_criteria:
        lines.append("## Success Criteria")
        lines.append("")
        for criterion in skill.success_criteria:
            lines.append(f"- {criterion}")
        lines.append("")

    # Backward compat: flat instructions from old-format skills
    if skill.instructions and not skill.steps:
        lines.append(skill.instructions.strip())
        lines.append("")

    # Scripts reference
    if skill.scripts:
        lines.append("## Scripts")
        lines.append("")
        lines.append(
            "Bundled scripts expose argument details via `--help` "
            "— prefer that over reading the source."
        )
        lines.append("")
        for script in skill.scripts:
            desc = f" — {script.description}" if script.description else ""
            if script.content:
                # Bundled script: render path under scripts/ using basename only.
                fname = Path(script.filename).name
                lines.append(f"- `${{SKILL_DIR}}/scripts/{fname}`{desc}")
            else:
                # External script reference — absolute path
                lines.append(f"- `{script.filename}`{desc}")
        lines.append("")

    # ── Write SKILL.md ──
    frontmatter_str = yaml.dump(
        frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False
    ).strip()
    body = "\n".join(lines)
    return f"---\n{frontmatter_str}\n---\n\n{body}\n"


def render_skill(skill: Skill, output_dir: Path) -> Path:
    """Render a Skill to disk as SKILL.md + scripts/.

    Returns the path to the SKILL.md file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    skill_md = render_skill_markdown(skill)

    md_path = output_dir / "SKILL.md"
    md_path.write_text(skill_md, encoding="utf-8")

    # ── Write figs/ ──
    if skill.figure_data:
        figs_dir = output_dir / "figs"
        figs_dir.mkdir(exist_ok=True)
        for filename, data in skill.figure_data.items():
            (figs_dir / filename).write_bytes(data)

    # ── Write scripts/ ──
    if skill.scripts:
        scripts_dir = output_dir / "scripts"
        for script in skill.scripts:
            if not script.content:
                continue  # external reference — nothing to write
            # Force basename: Path('/a') / '/b' == Path('/b'), so an absolute
            # `script.filename` would escape scripts_dir if joined directly.
            fname = Path(script.filename).name
            scripts_dir.mkdir(exist_ok=True)
            script_path = scripts_dir / fname
            script_path.write_text(script.content, encoding="utf-8")
            if fname.endswith((".sh", ".bash")):
                script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)

    return md_path


# Content block: either a text segment (str) or an image (bytes, mime_type).
ContentBlock = str | tuple[bytes, str]


def render_skill_for_llm(skill: Skill) -> list[ContentBlock]:
    """Render a skill to interleaved text + image content blocks.

    Returns a list where each element is either a text string or
    an (image_bytes, mime_type) tuple.  Images are placed immediately
    after the text that references them, so the model sees each figure
    in context rather than in a trailing batch.
    """
    import re

    md_text = render_skill_markdown(skill)

    if not skill.figure_data:
        return [md_text]

    fig_re = re.compile(r"!\[(.+?)\]\(figs/(.+?)\)")
    blocks: list[ContentBlock] = []
    last_end = 0

    for m in fig_re.finditer(md_text):
        caption = m.group(1)
        filename = m.group(2)
        data = skill.figure_data.get(filename)

        if data is None:
            continue  # no image data — keep original markdown (included in next text chunk)

        # Text before this figure (including the placeholder)
        text_before = (
            md_text[last_end:m.start()]
            + "[REFERENCE figure from original demonstration"
            f" — NOT a live screenshot: {caption}]"
        )
        if text_before.strip():
            blocks.append(text_before)

        ext = filename.rsplit(".", 1)[-1].lower()
        mime = "image/png" if ext == "png" else "image/jpeg"
        blocks.append((data, mime))
        last_end = m.end()

    # Remaining text after the last figure
    tail = md_text[last_end:]
    if tail.strip():
        blocks.append(tail)

    return blocks if blocks else [md_text]


def _title_case(kebab_name: str) -> str:
    """Convert kebab-case to Title Case."""
    return " ".join(word.capitalize() for word in kebab_name.split("-"))

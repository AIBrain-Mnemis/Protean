"""Skill registry — discover and load skills from SKILL.md files.

Supports bidirectional conversion:
  SKILL.md → Skill  (this module, parser)
  Skill → SKILL.md  (renderer module)
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import yaml

from protean.skills.schema import (
    Branch,
    Skill,
    SkillParameter,
    SkillRequires,
    SkillScript,
    Step,
    StepFigure,
    VerifyCondition,
    to_kebab,
)

log = logging.getLogger(__name__)


class SkillRegistry:
    """Registry for loading and managing skills from a directory."""

    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = skills_dir
        self._skills: dict[str, Skill] = {}
        self._paths: dict[str, Path] = {}

    def load_all(self) -> int:
        """Scan skills_dir for SKILL.md files, load them. Returns count loaded."""
        self._skills.clear()
        self._paths.clear()
        if not self._skills_dir.exists():
            return 0

        count = 0
        for skill_md in self._skills_dir.rglob("SKILL.md"):
            try:
                skill = load_skill_from_file(skill_md)
                self._skills[skill.name] = skill
                self._paths[skill.name] = skill_md.parent
                count += 1
            except Exception:
                log.warning("Failed to parse skill: %s", skill_md, exc_info=True)
        return count

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def get_path(self, name: str) -> Path | None:
        """Return the directory containing the skill's SKILL.md, or None."""
        return self._paths.get(name)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def search(self, query: str) -> list[Skill]:
        """Simple text search across skill name, description, and tags."""
        query_lower = query.lower()
        results = []
        for skill in self._skills.values():
            searchable = f"{skill.name} {skill.description} {' '.join(skill.tags)}".lower()
            if query_lower in searchable:
                results.append(skill)
        return results

    def delete(self, name: str) -> bool:
        """Delete a skill by name. Removes the directory from disk and cache.

        Returns True if the skill was found and deleted, False otherwise.
        """
        skill_dir = self._paths.get(name)
        if skill_dir is None:
            # Try finding by directory name convention
            candidate = self._skills_dir / name
            if candidate.exists() and (candidate / "SKILL.md").exists():
                skill_dir = candidate
            else:
                log.warning("delete: skill %r not found", name)
                return False

        if skill_dir.exists():
            shutil.rmtree(skill_dir)
            log.info("delete: removed skill directory %s", skill_dir)

        self._skills.pop(name, None)
        self._paths.pop(name, None)
        return True


def load_skill_from_file(path: Path) -> Skill:
    """Load a Skill from a SKILL.md file (YAML frontmatter + Markdown body).

    Supports both new structured format (## Steps with routes) and old flat
    Markdown format (plain instructions).
    """
    text = path.read_text(encoding="utf-8")

    # Parse YAML frontmatter
    frontmatter: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            frontmatter = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()

    name = frontmatter.get("name", path.parent.name)
    description = frontmatter.get("description", "")

    # Extract metadata
    meta = frontmatter.get("metadata", {})
    protean_meta = meta.get("protean", {})

    req_data = protean_meta.get("requires", {})
    requires = SkillRequires(
        bins=req_data.get("bins", []),
        env=req_data.get("env", []),
    )
    install_data = protean_meta.get("install", [])
    install: list[str] = []
    for i in install_data:
        if isinstance(i, str):
            install.append(i)
        elif isinstance(i, dict):
            # Backward compat: old {kind, package} format → "kind install package"
            install.append(f"{i.get('kind', '')} install {i.get('package', i.get('formula', ''))}")
    tags = meta.get("tags", [])

    # Preserve free-form metadata fields not consumed above
    _consumed_keys = {"requires", "install", "source", "related_skills"}
    free_metadata = {
        k: v for k, v in protean_meta.items() if k not in _consumed_keys
    }

    # Parse script metadata from ## Scripts section.
    # Content is NOT loaded here — only filename + description.
    # Content is loaded lazily from scripts/ dir when needed.
    script_descs = _parse_script_descriptions(body)
    scripts_dir = path.parent / "scripts"
    scripts: list[SkillScript] = []
    for filename, desc in script_descs.items():
        # Check if the script exists in the skill's scripts/ directory.
        # Use the bare name (strip any path prefix) for the filesystem check.
        bare_name = Path(filename).name
        script_file = scripts_dir / bare_name
        if script_file.is_file():
            # Embedded script — load content from scripts/ dir
            content = script_file.read_text(encoding="utf-8")
            scripts.append(SkillScript(
                filename=filename,
                content=content,
                description=desc,
            ))
        else:
            # External reference — no local content
            scripts.append(SkillScript(
                filename=filename,
                content="",
                description=desc,
            ))

    # Load figure images from disk
    figure_data: dict[str, bytes] = {}
    figs_dir = path.parent / "figs"
    if figs_dir.is_dir():
        for fig_file in sorted(figs_dir.iterdir()):
            if fig_file.is_file() and fig_file.suffix in (".jpg", ".jpeg", ".png"):
                figure_data[fig_file.name] = fig_file.read_bytes()

    # Parse structured body sections
    goal, steps, success_criteria, when_to_use, when_not_to_use, parameters, instructions = (
        _parse_body(body)
    )

    return Skill(
        name=name,
        description=description,
        goal=goal,
        steps=steps,
        success_criteria=success_criteria,
        when_to_use=when_to_use,
        when_not_to_use=when_not_to_use,
        parameters=parameters,
        requires=requires,
        install=install,
        scripts=scripts,
        tags=tags,
        instructions=instructions,
        source=protean_meta.get("source", ""),
        related_skills=protean_meta.get("related_skills", []),
        metadata=free_metadata,
        figure_data=figure_data,
    )


def _parse_body(
    body: str,
) -> tuple[str, list[Step], list[str], list[str], list[str], list[SkillParameter], str]:
    """Parse the Markdown body into structured fields.

    Returns (goal, steps, success_criteria, when_to_use, when_not_to_use,
             parameters, instructions).
    If the body doesn't have ## Steps, treats entire body as flat instructions.
    """
    sections = _split_sections(body)

    goal = sections.get("goal", "").strip()
    when_to_use = _parse_list(sections.get("when to use", ""))
    when_not_to_use = _parse_list(sections.get("when not to use", ""))
    success_criteria = _parse_list(sections.get("success criteria", ""))
    parameters = _parse_parameters(sections.get("inputs", ""))

    steps_text = sections.get("steps", "")
    steps = _parse_steps(steps_text) if steps_text.strip() else []

    # If no structured steps found, keep entire body as flat instructions
    instructions = "" if steps else body

    return (
        goal, steps, success_criteria, when_to_use, when_not_to_use,
        parameters, instructions,
    )


def _split_sections(body: str) -> dict[str, str]:
    """Split Markdown body by ## headers into {header_lower: content}."""
    sections: dict[str, str] = {}
    current_header = ""
    current_lines: list[str] = []

    for line in body.split("\n"):
        if line.startswith("## "):
            if current_header:
                sections[current_header] = "\n".join(current_lines)
            current_header = line[3:].strip().lower()
            current_lines = []
        else:
            current_lines.append(line)

    if current_header:
        sections[current_header] = "\n".join(current_lines)

    return sections


def _parse_list(text: str) -> list[str]:
    """Parse a section of '- item' lines into a list of strings."""
    items = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
    return items


def _parse_steps(steps_text: str) -> list[Step]:
    """Parse ### numbered step subsections into Step objects."""
    steps: list[Step] = []
    step_blocks = re.split(r"^### \d+\.\s*", steps_text, flags=re.MULTILINE)

    for block in step_blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.split("\n")
        # Heading is title-cased step name, convert back to kebab-case
        heading = lines[0].strip()
        step_name = to_kebab(heading)
        rest = "\n".join(lines[1:])

        tool = _extract_field(rest, "Tool")
        tool = tool.strip("`")
        target_app = _extract_field(rest, "App")
        idempotent_raw = _extract_field(rest, "Idempotent")
        idempotent = idempotent_raw.lower() not in ("no", "false") if idempotent_raw else True

        # Parse verify_condition block
        verify_condition = _parse_verify_condition(rest)
        # Fold **Verify:** text into verify_condition.description if not already set
        verify_text = _extract_field(rest, "Verify")
        if verify_text:
            if verify_condition and not verify_condition.description:
                verify_condition.description = verify_text
            elif not verify_condition:
                verify_condition = VerifyCondition(
                    strategy="visual", description=verify_text,
                )

        # Parse branch lines:
        # **Branch:** if strategy=ax_element, description=..., ax_role=... → `step`
        branches: list[Branch] = []
        for line in rest.strip().split("\n"):
            stripped = line.strip()
            br_match = re.match(
                r"\*\*Branch:\*\*\s+if\s+(.+?)\s+→\s+`([^`]+)`",
                stripped,
            )
            if br_match:
                vc_fields = _parse_kv_inline(br_match.group(1))
                strategy = vc_fields.get("strategy", "visual")
                if strategy not in ("ax_element", "text_content", "visual"):
                    strategy = "visual"
                branches.append(Branch(
                    condition=VerifyCondition(
                        strategy=strategy,  # type: ignore[arg-type]
                        description=vc_fields.get("description", ""),
                        ax_role=vc_fields.get("ax_role", ""),
                        ax_title=vc_fields.get("ax_title", ""),
                        expected_text=vc_fields.get(
                            "expected_text", "",
                        ),
                    ),
                    next_step=br_match.group(2),
                ))

        # Action is the remaining text (not metadata lines)
        _skip_prefixes = (
            "**Tool:**", "**Verify:**", "**Idempotent:**",
            "**App:**", "**Verify Condition:**",
            "**Branch:**",
        )
        action_lines = []
        figures: list[StepFigure] = []
        skip_vc_block = False
        for line in rest.strip().split("\n"):
            stripped = line.strip()
            if any(stripped.startswith(p) for p in _skip_prefixes):
                if stripped.startswith("**Verify Condition:**"):
                    skip_vc_block = True
                continue
            # Lines indented under Verify Condition (e.g. "  ax_role=...")
            if skip_vc_block:
                if stripped.startswith(("ax_role=", "ax_title=", "expected_text=", "description=")):
                    continue
                skip_vc_block = False
            # Extract figure references: ![caption](figs/filename)
            fig_match = re.match(r"^!\[(.+?)\]\(figs/(.+?)\)\s*$", stripped)
            if fig_match:
                figures.append(StepFigure(ref=fig_match.group(2), caption=fig_match.group(1)))
                continue
            action_lines.append(line)
        action = "\n".join(action_lines).strip()

        steps.append(Step(
            name=step_name,
            action=action,
            tool=tool,
            target_app=target_app,
            verify_condition=verify_condition,
            idempotent=idempotent,
            figures=figures,
            branches=branches,
        ))

    return steps


def _parse_verify_condition(text: str) -> VerifyCondition | None:
    """Parse a **Verify Condition:** block into a VerifyCondition.

    Expected format:
        **Verify Condition:** strategy=ax_element
          ax_role=AXButton
          ax_title=OK
    """
    vc_raw = _extract_field(text, "Verify Condition")
    if not vc_raw:
        return None

    # First line has strategy=... (may also have other fields inline)
    fields: dict[str, str] = {}
    # Parse "strategy=ax_element" from the first line
    for part in vc_raw.split():
        if "=" in part:
            k, _, v = part.partition("=")
            fields[k.strip()] = v.strip()

    # Parse continuation lines (indented, like "  ax_role=AXButton")
    in_block = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("**Verify Condition:**"):
            in_block = True
            continue
        if in_block:
            if "=" in stripped and not stripped.startswith("**"):
                k, _, v = stripped.partition("=")
                fields[k.strip()] = v.strip()
            elif stripped.startswith("**") or (
                stripped and not stripped[0].isspace() and "=" not in stripped
            ):
                break
            elif not stripped:
                break

    strategy = fields.get("strategy", "")
    if strategy not in ("ax_element", "visual", "text_content"):
        return None

    return VerifyCondition(
        strategy=strategy,  # type: ignore[arg-type]
        ax_role=fields.get("ax_role", ""),
        ax_title=fields.get("ax_title", ""),
        expected_text=fields.get("expected_text", ""),
        description=fields.get("description", ""),
    )


def _extract_field(text: str, field_name: str) -> str:
    """Extract a **FieldName:** value from text."""
    pattern = rf"\*\*{field_name}:\*\*\s*(.+)"
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""


def _parse_parameters(text: str) -> list[SkillParameter]:
    """Parse ## Inputs section into SkillParameter list.

    Format: - **name** (required): description (default: `value`) — constraints
    """
    params: list[SkillParameter] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        m = re.match(
            r"^- \*\*(\w+)\*\*\s+\((\w+)\):\s+(.+)$",
            line,
        )
        if not m:
            continue
        name = m.group(1)
        req_str = m.group(2)
        rest = m.group(3)

        required = req_str == "required"
        default = ""
        constraints = ""

        # Extract (default: `value`)
        default_m = re.search(r"\(default:\s*`([^`]*)`\)", rest)
        if default_m:
            default = default_m.group(1)
            rest = rest[:default_m.start()] + rest[default_m.end():]

        # Extract — constraints
        if " — " in rest:
            desc_part, constraints = rest.split(" — ", 1)
            rest = desc_part

        params.append(SkillParameter(
            name=name,
            description=rest.strip(),
            required=required,
            default=default,
            constraints=constraints.strip(),
        ))
    return params


def _parse_kv_inline(text: str) -> dict[str, str]:
    """Parse 'key=value, key=value' inline format into a dict."""
    result: dict[str, str] = {}
    for part in re.split(r",\s*", text):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip()] = v.strip()
    return result


def _parse_script_descriptions(body: str) -> dict[str, str]:
    """Parse ## Scripts section to extract filename → description.

    Two formats:
    - Created:  - `${SKILL_DIR}/scripts/run.sh` — Main runner
    - External: - `/opt/tools/check_disk.py` — Checks disk status
    """
    sections = _split_sections(body)
    scripts_text = sections.get("scripts", "")
    descs: dict[str, str] = {}
    for line in scripts_text.strip().split("\n"):
        # Created script: ${SKILL_DIR}/scripts/filename
        m = re.match(
            r"^-\s+`\$\{SKILL_DIR\}/scripts/(.+?)`(?:\s+—\s+(.+))?$",
            line.strip(),
        )
        if m:
            descs[m.group(1)] = (m.group(2) or "").strip()
            continue
        # External script: absolute or ~/ path
        m = re.match(
            r"^-\s+`([/~][^`]+)`(?:\s+—\s+(.+))?$",
            line.strip(),
        )
        if m:
            descs[m.group(1)] = (m.group(2) or "").strip()
            continue
        # Bare filename (no path prefix) — referenced external script
        m = re.match(
            r"^-\s+`([^`/~\$][^`]*)`(?:\s+—\s+(.+))?$",
            line.strip(),
        )
        if m:
            descs[m.group(1)] = (m.group(2) or "").strip()
    return descs

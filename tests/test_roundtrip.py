"""Test round-trip: Skill → SKILL.md → parsed Skill."""

import tempfile
from pathlib import Path

from protean.skills.registry import load_skill_from_file
from protean.skills.renderer import render_skill
from protean.skills.schema import Branch, Skill, SkillParameter, Step, StepFigure, VerifyCondition


def test_roundtrip():
    skill = Skill(
        name="export-pdf",
        description="Export document as PDF. Use when: saving files as PDF.",
        goal="Export the currently open document as a PDF file.",
        steps=[
            Step(
                name="open-export-dialog",
                action="Press Cmd+Shift+E in Pixelmator Pro to open the export dialog.",
                tool="key_press",
                target_app="Pixelmator Pro",
                verify_condition=VerifyCondition(
                    strategy="visual", description="Export dialog is visible",
                ),
                figures=[StepFigure(ref="overview_1.jpg", caption="Export dialog opened")],
            ),
            Step(
                name="select-pdf-format",
                action="Click the format dropdown and select PDF in Pixelmator Pro.",
                tool="click",
                target_app="Pixelmator Pro",
                verify_condition=VerifyCondition(
                    strategy="text_content",
                    expected_text="PDF",
                    description="Format shows PDF",
                ),
                figures=[
                    StepFigure(ref="detail_2.jpg", caption="Format dropdown showing PDF"),
                    StepFigure(ref="overview_2.jpg", caption="Full view with PDF selected"),
                ],
                branches=[
                    Branch(
                        condition=VerifyCondition(
                            strategy="ax_element",
                            ax_role="AXSheet",
                            description="Confirmation dialog appears",
                        ),
                        next_step="dismiss-dialog",
                    ),
                ],
            ),
            Step(
                name="dismiss-dialog",
                action="Click OK on the confirmation dialog.",
                target_app="Pixelmator Pro",
            ),
            Step(
                name="save-the-file",
                action="Set filename to {{filename}}.pdf and click Save.",
                tool="",
                verify_condition=VerifyCondition(
                    strategy="visual", description="File exists at path",
                ),
                idempotent=False,
            ),
        ],
        success_criteria=["PDF exists", "File size > 0"],
        when_to_use=["export as PDF", "save PDF"],
        when_not_to_use=["batch export"],
        parameters=[
            SkillParameter(name="filename", description="Output filename"),
            SkillParameter(
                name="quality",
                description="Export quality",
                required=False,
                default="high",
                constraints="one of: low, medium, high",
            ),
        ],
        tags=["pdf", "export"],
        related_skills=["open-document", "print-pdf"],
        figure_data={
            "overview_1.jpg": b"fake-overview-1",
            "detail_2.jpg": b"fake-detail-2",
            "overview_2.jpg": b"fake-overview-2",
        },
    )

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "export-pdf"
        md_path = render_skill(skill, out)

        # Verify SKILL.md was written
        content = md_path.read_text()
        assert "## Steps" in content
        assert "Open Export Dialog" in content
        assert "Export dialog is visible" in content
        assert "`key_press`" in content
        assert "![Export dialog opened](figs/overview_1.jpg)" in content
        assert "![Format dropdown showing PDF](figs/detail_2.jpg)" in content

        # Verify figs/ were written
        figs_dir = out / "figs"
        assert figs_dir.is_dir()
        assert (figs_dir / "overview_1.jpg").read_bytes() == b"fake-overview-1"
        assert (figs_dir / "detail_2.jpg").read_bytes() == b"fake-detail-2"
        assert (figs_dir / "overview_2.jpg").read_bytes() == b"fake-overview-2"

        # Parse back
        parsed = load_skill_from_file(md_path)

        assert parsed.name == "export-pdf"
        assert parsed.goal == "Export the currently open document as a PDF file."
        assert len(parsed.steps) == 4

        # Step 0: basic fields + figures
        assert parsed.steps[0].name == "open-export-dialog"
        assert parsed.steps[0].tool == "key_press"
        assert parsed.steps[0].target_app == "Pixelmator Pro"
        assert parsed.steps[0].verify_condition.description == "Export dialog is visible"
        assert len(parsed.steps[0].figures) == 1
        assert parsed.steps[0].figures[0].ref == "overview_1.jpg"
        assert parsed.steps[0].figures[0].caption == "Export dialog opened"

        # Step 1: verify_condition with strategy fields + branch
        s1 = parsed.steps[1]
        assert s1.tool == "click"
        assert s1.verify_condition.strategy == "text_content"
        assert s1.verify_condition.expected_text == "PDF"
        assert len(s1.figures) == 2
        assert s1.figures[0].ref == "detail_2.jpg"
        assert s1.figures[1].caption == "Full view with PDF selected"
        assert len(s1.branches) == 1
        br = s1.branches[0]
        assert br.next_step == "dismiss-dialog"
        assert br.condition.strategy == "ax_element"
        assert br.condition.ax_role == "AXSheet"
        assert br.condition.description == "Confirmation dialog appears"

        # Step 3: idempotent=False
        assert parsed.steps[3].idempotent is False
        assert "{{filename}}" in parsed.steps[3].action

        # Parameters with constraints
        assert len(parsed.parameters) == 2
        assert parsed.parameters[0].name == "filename"
        p1 = parsed.parameters[1]
        assert p1.name == "quality"
        assert p1.required is False
        assert p1.default == "high"
        assert p1.constraints == "one of: low, medium, high"

        # Skill-level fields
        assert parsed.success_criteria == ["PDF exists", "File size > 0"]
        assert parsed.when_to_use == ["export as PDF", "save PDF"]
        assert parsed.when_not_to_use == ["batch export"]
        assert parsed.tags == ["pdf", "export"]
        assert parsed.related_skills == ["open-document", "print-pdf"]
        assert parsed.instructions == ""

        # Verify figure_data round-trips through load
        assert parsed.figure_data["overview_1.jpg"] == b"fake-overview-1"
        assert parsed.figure_data["detail_2.jpg"] == b"fake-detail-2"
        assert parsed.figure_data["overview_2.jpg"] == b"fake-overview-2"

        # Verify render_skill_for_llm produces images list
        from protean.skills.renderer import render_skill_for_llm

        blocks = render_skill_for_llm(parsed)
        # 3 figures total across steps → at least 3 image blocks
        image_blocks = [b for b in blocks if isinstance(b, tuple)]
        text_blocks = [b for b in blocks if isinstance(b, str)]
        assert len(image_blocks) == 3
        assert all(mime in ("image/jpeg", "image/png") for _, mime in image_blocks)
        # ![...](figs/...) replaced with [REFERENCE figure ...] in text blocks
        combined_text = "\n".join(text_blocks)
        assert "Export dialog opened]" in combined_text
        assert "Format dropdown showing PDF]" in combined_text
        assert "![" not in combined_text


def test_old_format_backward_compat():
    """Old-format SKILL.md (flat Markdown, no ## Steps) should still load."""
    with tempfile.TemporaryDirectory() as d:
        md_path = Path(d) / "old-skill" / "SKILL.md"
        md_path.parent.mkdir()
        md_path.write_text(
            "---\nname: old-skill\ndescription: An old skill\n---\n\n"
            "# Old Skill\n\nJust do the thing.\n\n## How\n\n1. Step one\n2. Step two\n"
        )

        skill = load_skill_from_file(md_path)
        assert skill.name == "old-skill"
        assert skill.steps == []
        assert "Just do the thing" in skill.instructions


# ── validate_steps tests ─────────────────────────────────


def test_validate_steps_valid():
    skill = Skill(
        name="valid",
        description="test",
        steps=[
            Step(name="step-a", action="Do A."),
            Step(name="step-b", action="Do B.",
                 branches=[Branch(
                     condition=VerifyCondition(
                         strategy="ax_element", ax_role="AXSheet",
                         description="dialog",
                     ),
                     next_step="step-a",
                 )]),
        ],
    )
    assert skill.validate_steps() == []


def test_validate_steps_missing_name():
    skill = Skill(
        name="bad",
        description="test",
        steps=[Step(name="", action="Do A.")],
    )
    errors = skill.validate_steps()
    assert len(errors) == 1
    assert "missing name" in errors[0]


def test_validate_steps_duplicate_name():
    skill = Skill(
        name="bad",
        description="test",
        steps=[
            Step(name="dup", action="First."),
            Step(name="dup", action="Second."),
        ],
    )
    errors = skill.validate_steps()
    assert len(errors) == 1
    assert "duplicate" in errors[0]


def test_validate_steps_bad_branch_target():
    skill = Skill(
        name="bad",
        description="test",
        steps=[
            Step(name="step-a", action="Do A.",
                 branches=[Branch(
                     condition=VerifyCondition(
                         strategy="visual", description="x",
                     ),
                     next_step="nonexistent",
                 )]),
        ],
    )
    errors = skill.validate_steps()
    assert len(errors) == 1
    assert "nonexistent" in errors[0]

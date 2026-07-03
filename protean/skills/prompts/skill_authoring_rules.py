"""Shared skill-level authoring rules embedded into skill-writing prompts."""

SKILL_AUTHORING_RULES = """\
## Skill shape

- Write the skill as a compact fast path with conditional branches for observed risks. Include checks that prove correctness-critical requirements, but avoid duplicating the same constraint across many sections. Put rare or costly validation behind explicit triggers so future executions can stay concise when the risk is absent.
- Use progressive disclosure for script-backed skills. `SKILL.md` should contain the critical decision rules, invariants, fast path, and minimal command shapes a future agent needs to start correctly. Detailed flag catalogs, edge-case command syntax, troubleshooting notes, and exhaustive option descriptions belong in the bundled script's `--help` output or docstring, not repeated in `SKILL.md`.
- Separate skill-section responsibilities. `parameters`/rendered `Inputs` should define runtime variables, required constraints, and value sources only when they affect execution; `when_to_use` should hold invocation triggers; steps should hold decision and action logic; `success_criteria` should hold independent end-state acceptance checks for the final verifier. Do not copy helper manuals, step rationale, or the same invariant across sections; keep the canonical wording where it drives a decision and reference it tersely elsewhere.

"""  # noqa: E501

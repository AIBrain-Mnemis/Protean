"""Prompt for finalizing a skill from a sequence of demonstration steps."""

from protean.skills.prompts.skill_authoring_rules import SKILL_AUTHORING_RULES
from protean.skills.prompts.step_field_rules import STEP_FIELD_RULES

FINALIZE_PROMPT = """\
You are given a sequence of steps from a task demonstration.
Rewrite them into a final skill.

Requirements:
- Keep the same step order.
- Keep roughly the same number of logical steps unless a step is clearly redundant.
- Convert the demonstration into a reusable skill, not a one-off recap.
- A step should be a logical workflow step, not an atomic low-level UI action.
- **Treat the demonstration as evidence of intent, not a script to replay.**
  For each logical step, ask: what is the user actually trying to achieve,
  and what is the most efficient reliable way to achieve it? Substitute a
  shell command, script, or keyboard shortcut for a long GUI sequence
  whenever it produces the same observable end state and is well-known to
  work (e.g. a `sed` one-liner replacing an editor find/replace dance).
  When uncertain, keep the demonstrated GUI path. Never fabricate a
  shortcut that wasn't demonstrated to work.
- Incorporate user feedback when present.
- Generate skill metadata that matches these rewritten steps.
- If a value can vary between runs, define it as a parameter and reuse that same
    parameter consistently everywhere it appears.
- Once you define a parameter, replace the literal value with {{param_name}} in
    description, goal, steps, and success_criteria. Do not leave hard-coded copies.
- Keep only stable product/UI labels literal. Variable business values such as
    titles, names, rooms, dates, times, and search text should usually become parameters.

""" + SKILL_AUTHORING_RULES + STEP_FIELD_RULES + """
Here are the steps:

"""

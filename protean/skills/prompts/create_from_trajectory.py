"""Prompt for creating a new skill from an autonomous agent's trajectory."""

from protean.skills.prompts.step_field_rules import STEP_FIELD_RULES

CREATE_FROM_TRAJECTORY_PROMPT = """\
You are creating a new reusable skill from an agent's execution trajectory.

The agent was given a task and executed it autonomously. The trajectory records
every tool call, command, and observation the agent made. Your job is to distill
this into a clean, reusable skill that a future agent can follow to solve similar
tasks faster and more reliably. Skip dead-end retries but capture what finally
worked and why earlier attempts failed.

The trajectory is ONE concrete execution. The skill must generalize. Do NOT bake
concrete instances from this run — specific people, rooms, file paths, dates,
organization names, project ids — into the skill's ``description``, ``goal``,
``when_to_use``, step actions, or ``success_criteria``. Express those slots as
parameters or as schematic placeholders. The resulting skill should read as if
it could run on any equivalent input next week, not as a log of what happened
today.

""" + STEP_FIELD_RULES + """
## Task context

{task_context}

## Execution trajectory

(see interleaved content below)

Output a new skill.
"""

"""Prompt for refining an existing skill against an execution trajectory."""

from protean.skills.prompts.skill_authoring_rules import SKILL_AUTHORING_RULES
from protean.skills.prompts.step_field_rules import STEP_FIELD_RULES

REFINE_PROMPT = """\
You are refining an existing skill based on its real execution trajectory.

A skill starts as a rough sketch from one demonstration. The executor (an AI agent) tried to follow it and recorded everything it did. The trajectory reveals what the real UI actually looks like — exact labels, element locations, AX roles, unexpected dialogs. Use these real-world observations to make the skill precise enough that the executor can follow it without guessing next time.

## Rules
- Keep all original steps in order. Do NOT remove or rename existing steps.
- You may add new steps as branch targets for paths the original didn't handle.
- Keep skill `name` unchanged. Everything else can be improved.
- **Preserve figure references.** Keep existing figure refs in steps unless the step's action changed so fundamentally that the figure no longer applies. Only use refs from the available list — do NOT invent new refs.
- **Precision vs generalizability**: The trajectory is ONE execution on ONE machine. Add details that help the executor find the right element, but keep the skill general (see Generalizability rules in Step fields below). The refined skill must work for ALL future runs.
- **Do not pollute with concrete instances.** Specific people, rooms, file paths, dates, organization names, and project ids from THIS trajectory must not creep into the skill's ``description``, ``goal``, ``when_to_use``, step actions, or ``success_criteria``. If the original skill is already polluted with such instances, treat removing them as part of this refine.
- **Bound growth.** Before adding new content to a refine, scan existing sections for compression first: tighten run-on prose in `description`, `goal`, and `verify_condition.description`; fold conditions duplicated across `when_to_use` and `success_criteria`; drop dead clauses. Never drop a `when_to_use` trigger, `success_criteria` bullet, or step `action` detail just to make room.
- **Tighten cost-heavy successful paths.** When a successful trajectory spends substantial context or tool calls on long skill text, broad helper output, helper help/source inspection, ad hoc probes, or repeated broad validation, refine the skill into a compact fast path with trigger-based escalation. Keep checks that prove the final artifact, stated requirements, target selection, edit scope, and domain-critical values such as formulas, caches, units, directions, ordering, or immutability. Move rare-risk checks behind explicit triggers, such as archived/live ambiguity, same-sheet immutability risk, failed compact verification, missing metadata, or helper syntax uncertainty.
- **Operationalize causal evidence.** Evolution guidance may include factual evidence of why the trajectory succeeded or failed. When an evidence item identifies a reusable decision rule, inference strategy, recovery pattern, or validation gap, encode that causal pattern as an executable step action, branch, tool hint, verify condition, or success criterion. Preserve the general rule behind the evidence while removing incidental task-specific names and values.

## What to look for in the trajectory

The executor's actions reveal what the skill description was missing. For each step:

### 1. Ground the action in what the executor actually did
The original action may be vague ("open the meeting form"). The trajectory shows what the executor actually clicked, searched for, and found. Rewrite the action with those concrete details, following the Generalizability rules (real label + meaning + location). Also add any preceding navigation the executor had to do that the skill didn't mention ("first scroll down to reveal the section").

### 2. Add the WHY when the step purpose isn't obvious
If a step seems arbitrary without context, add a brief rationale in the action: "Click 'More options' (the three-dot menu) to reveal the recurrence settings which are hidden by default"

### 3. Capture the tool chain that worked
The trajectory shows what the executor clicked, typed, and pressed. Translate those actions into a tool hint following the Tool hints and Generalizability rules — identify elements by label, not coordinates.

### 4. Fix verify conditions from real verification results
The trajectory shows what the verifier actually found (or didn't find) on screen. If verification failed because:
- The AX element doesn't exist → change strategy or fix role/title
- The expected text was wrong → use the text that actually appeared
- The element exists but with a different role → fix the role
If verification passed, the current condition is correct — leave it.

### 5. Handle unexpected UI states
If the trajectory shows the executor encountered something the skill didn't predict (a confirmation dialog, a loading spinner that needed waiting, a permission prompt), add a branch to handle it.

### 6. Fix steps that needed assistant intervention
These are the highest priority. The assistant had to step in because the skill wasn't detailed enough. The trajectory shows what the assistant corrected. Incorporate that correction into the skill so it won't need help next time.

### 7. Tighten slow steps — even if they passed
If a step has many executor actions (tool calls), the executor was struggling internally — trying different queries, clicking wrong elements, retrying. Even though verification passed, the step is slow and fragile. Look at what the executor tried first (and failed) vs what finally worked. Rewrite the action and tool hint so the executor finds the right target on the first try.

### 8. Replace fragile GUI sequences with commands when reliable
If a step is a long or fragile GUI chain (many clicks, brittle find_elements queries, or repeated retries) AND the same end state can be produced by a verified shell command, script, or keyboard shortcut, rewrite the step to use that more direct route via run_terminal_command / run_script / key_press. Examples: a manual find-and-replace dance becomes `sed -i ...`; clicking through Finder/Explorer to create folders becomes `mkdir -p`; a Git GUI commit becomes `git commit -am ...`. Only substitute when you are confident the command achieves the same observable result on the target platform — otherwise keep the GUI path and just tighten it.

""" + SKILL_AUTHORING_RULES + STEP_FIELD_RULES + """
## Current skill

{skill_steps}

## Execution trajectory

(see interleaved content below)

Output the refined skill.
""" # noqa: E501

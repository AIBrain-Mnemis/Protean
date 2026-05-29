"""Prompt for the skill evolution router.

Decides whether to create / refine / delete skills in the library based on
an execution trajectory.
"""

EVOLVE_ROUTER_PROMPT = """\
You are a skill evolution router.

Given an execution trajectory and the current skill library, decide how the skill library should change. A skill should represent a reusable capability, strategy, workflow, debugging heuristic, or validation pattern that transfers across many tasks. Prefer general techniques over task-specific procedures.

## Execution trajectory
{trajectory_summary}

## Skills used in this execution
{used_skills}

## Task result
- Task: {task_name}
- Reward: {reward}
- Failed tests: {failed_tests}

## Decision rules

Trajectories may contain reusable knowledge even when the task failed.

Failures can reveal:
- useful partial workflows,
- debugging strategies,
- common pitfalls,
- verification techniques,
- or corrective patterns.

For each action, decide exactly one of:

Each action is a learning contract, not a draft skill. Fill these fields:

- skill_name: the stable skill key. For create, use kebab-case and name the
  underlying reusable capability, not incidental task details.
- reason: why this library operation is appropriate.
- intent: the focused change this action asks the builder to make. For create,
  this is the target capability. For refine, this is the desired update to the
  existing skill. For delete, this is the deprecation/removal intent.
- observed_gap: the missing, incorrect, redundant, or underspecified capability
  in the current library.
- evidence: 3-8 short factual observations from the trajectory. Evidence must
  point to user messages, tool calls, tool results, errors, corrections,
  recovery steps, or verification outcomes.

### refine

Use when an existing skill already overlaps with the capability revealed by the trajectory, even if the overlap is partial.

Refine when:
- an existing skill missed an edge case,
- the strategy was incomplete,
- the validation logic was insufficient,
- or the trajectory reveals a better generalized version of the skill.

Prefer refining broader skills instead of creating narrowly specialized ones.

### create

Use only when the trajectory reveals a reusable capability that is not already covered by an existing skill.

A new skill should capture:
- a transferable strategy,
- workflow,
- debugging heuristic,
- validation pattern,
- or transformation technique

that can help solve multiple unrelated tasks.

Do not create skills tied to:
- specific organizations,
- datasets,
- entities,
- file names,
- or one-off procedures.

The skill name should describe the underlying capability rather than the surface task.

### delete

Use only when a skill is:
- incorrect,
- harmful,
- redundant,
- or fully superseded by another skill.

Delete conservatively.

Output structured decisions only.
"""  # noqa: E501

"""Skill evolution — evolve a skill library from agent execution trajectories.

Entry point for the SkillFlow integration. After each task trial, call
``SkillEvolver.evolve()`` to route the trajectory to the right action
(create / refine / delete) and update the skill directory accordingly.

Architecture:
  - TrajectoryAdapter: ATIF trajectory (SkillFlow) → RunTrajectory (Protean)
  - SkillEvolver: orchestrates the evolution loop
    - _route(): LLM decides what actions to take
    - dispatches to SkillBuilder.from_trajectory / refine / registry.delete
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from protean.skills.builder import SkillBuilder, render_action_transcript
from protean.skills.registry import SkillRegistry
from protean.skills.renderer import render_skill
from protean.skills.runner import ExecutorAction, RunTrajectory, StepTrajectory

if TYPE_CHECKING:
    from protean.llm import LLM

log = logging.getLogger(__name__)


# ── Diff helpers ─────────────────────────────────────────


def _snapshot_skill_dir(skill_dir: Path) -> dict[str, Any]:
    """Capture SKILL.md text and scripts-dir listing (basename → sha256)."""
    md_path = skill_dir / "SKILL.md"
    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    scripts: dict[str, str] = {}
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.is_dir():
        for p in sorted(scripts_dir.iterdir()):
            if p.is_file():
                scripts[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return {"skill_md": md_text, "scripts": scripts}


def _compute_skill_diff(
    before: dict[str, Any], after: dict[str, Any],
) -> dict[str, Any]:
    """Build a JSON-friendly diff from two snapshots."""
    before_md: str = before.get("skill_md", "") or ""
    after_md: str = after.get("skill_md", "") or ""
    md_diff = "\n".join(
        difflib.unified_diff(
            before_md.splitlines(),
            after_md.splitlines(),
            fromfile="SKILL.md.before",
            tofile="SKILL.md.after",
            lineterm="",
            n=3,
        ),
    )

    before_scripts: dict[str, str] = before.get("scripts", {}) or {}
    after_scripts: dict[str, str] = after.get("scripts", {}) or {}
    before_names = set(before_scripts)
    after_names = set(after_scripts)
    added = sorted(after_names - before_names)
    removed = sorted(before_names - after_names)
    modified = sorted(
        name for name in before_names & after_names
        if before_scripts[name] != after_scripts[name]
    )

    return {
        "skill_md_diff": md_diff,
        "skill_md_changed": before_md != after_md,
        "scripts_added": added,
        "scripts_removed": removed,
        "scripts_modified": modified,
    }


# ── Trajectory adapter ───────────────────────────────────


class TrajectoryAdapter:
    """Convert ATIF trajectory (SkillFlow format) to Protean RunTrajectory."""

    @staticmethod
    def from_atif(
        atif: dict[str, Any],
    ) -> tuple[list[ExecutorAction], str, str]:
        """Extract actions, instruction, and final response from ATIF trajectory.

        Returns (actions, instruction, final_response) — the caller
        assembles these into a RunTrajectory with its own verify info.
        """
        steps = atif.get("steps", [])
        actions: list[ExecutorAction] = []
        instruction = ""
        final_response = ""

        for step in steps:
            source = step.get("source", "")
            msg = step.get("message", "") or ""

            if source == "user":
                if not instruction:
                    instruction = msg
                elif msg:
                    # Subsequent user steps are injected content
                    # (e.g. Claude auto-injects document base64)
                    actions.append(ExecutorAction(
                        result=msg,
                        event_type="tool_result",
                    ))
                continue

            if source != "agent":
                continue

            if msg:
                actions.append(ExecutorAction(
                    result=msg,
                    event_type="message",
                ))
                final_response = msg

            # Tool calls
            for tc in step.get("tool_calls", []):
                fn = tc.get("function_name", "")
                args = tc.get("arguments") or {}
                actions.append(ExecutorAction(
                    tool_name=fn,
                    tool_args=args if isinstance(args, dict) else {},
                    event_type="tool_call",
                ))

            # Tool results from observation
            for r in (step.get("observation") or {}).get("results", []):
                content = r.get("content", "")
                if content:
                    actions.append(ExecutorAction(
                        result=content,
                        event_type="tool_result",
                    ))

        return actions, instruction, final_response

    @staticmethod
    def to_run_trajectory(
        actions: list[ExecutorAction],
        instruction: str,
        final_response: str,
        *,
        task_name: str = "",
        verify_passed: bool = False,
        verify_reason: str = "",
        reward: float | None = None,
        failed_tests: list[str] = [],
        exception_type: str = "",
        exception_message: str = "",
    ) -> RunTrajectory:
        """Assemble a RunTrajectory from extracted actions and verify info.

        When *reward*, *failed_tests*, or *exception_type/exception_message*
        are supplied they are folded into *verify_reason* so the downstream
        builder prompt can see them without needing dedicated parameters.
        """
        parts: list[str] = []
        if verify_reason:
            parts.append(verify_reason)
        if reward is not None:
            parts.append(f"Reward: {reward}")
        if failed_tests:
            parts.append(f"Failed tests: {', '.join(failed_tests)}")
        if exception_type:
            parts.append(
                f"Exception: {exception_type}: {exception_message}"
                if exception_message
                else f"Exception: {exception_type}"
            )
        combined_reason = ". ".join(parts) if parts else ""

        return RunTrajectory(
            skill_name=task_name,
            task=instruction,
            steps=[StepTrajectory(
                step_index=-1,
                step_name="full-execution",
                instruction=instruction,
                actions=actions,
                executor_response=final_response,
                verify_strategy="pytest",
                verify_passed=verify_passed,
                verify_reason=combined_reason,
            )],
        )

    @staticmethod
    def from_atif_file(
        trajectory_path: Path,
    ) -> tuple[list[ExecutorAction], str, str]:
        """Load an ATIF trajectory from file and extract actions."""
        data = json.loads(trajectory_path.read_text(encoding="utf-8"))
        return TrajectoryAdapter.from_atif(data)


# ── Router models ────────────────────────────────────────


class EvolveAction(BaseModel):
    """A single evolution action decided by the router."""

    action: Literal["create", "refine", "delete"] = Field(
        description="What to do: create a new skill, refine an existing one, "
        "or delete an obsolete one.",
    )
    skill_name: str = Field(
        description="For 'refine'/'delete': name of the existing skill. "
        "For 'create': suggested name for the new skill (kebab-case).",
    )
    reason: str = Field(
        description="Why this action is needed, based on the trajectory.",
    )


class EvolveDecision(BaseModel):
    """Router output: list of actions to take on the skill library."""

    actions: list[EvolveAction] = Field(
        description="Ordered list of skill evolution actions.",
    )
    reasoning: str = Field(
        default="",
        description="Brief explanation of the overall evolution strategy.",
    )


_ROUTER_PROMPT = """\
You are a skill evolution router.

Given an execution trajectory and the current skill library, decide how the skill library should change. A skill should represent a reusable capability, strategy, workflow, debugging heuristic, or validation pattern that transfers across many tasks. Prefer general techniques over task-specific procedures.

## Execution trajectory
{trajectory_summary}

## Skills used in this exexecution
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
""" # noqa: E501


# ── Evolver ──────────────────────────────────────────────


@dataclass
class EvolveResult:
    """Result of a single evolve() call."""

    actions_taken: list[dict[str, Any]]
    skills_created: list[str]
    skills_refined: list[str]
    skills_deleted: list[str]


class SkillEvolver:
    """Orchestrate skill evolution from agent trajectories.

    Usage:
        from protean.llm import LLM
        from protean.skills.evolve import SkillEvolver

        evolver = SkillEvolver(skills_dir, llm)
        result = await evolver.evolve(trajectory, task_result)
    """

    def __init__(
        self,
        skills_dir: Path,
        llm: "LLM",
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> None:
        self._skills_dir = skills_dir
        self._llm = llm
        self._model = model
        self._temperature = temperature
        self._registry = SkillRegistry(skills_dir)
        self._builder = SkillBuilder()

    async def evolve(
        self,
        trajectory: RunTrajectory,
        *,
        task_name: str = "",
        reward: float | None = None,
        failed_tests: list[str] | None = None,
        used_skills: list[str] | None = None,
        task_context: str = "",
    ) -> EvolveResult:
        """Run the full evolution loop: route → dispatch → write.

        Args:
            trajectory: Protean RunTrajectory (use TrajectoryAdapter to convert).
                Run-level metadata (task, verify_passed, verify_reason) is
                read from the trajectory itself.
            task_name: Name of the task (falls back to trajectory.skill_name).
            reward: Verifier reward (0.0-1.0). If None, extracted from
                trajectory.verify_reason if available.
            failed_tests: Names of failed test cases.
            used_skills: Skills the agent referenced during execution.
            task_context: Extra task context for skill creation. If empty,
                falls back to trajectory.task.

        Returns:
            EvolveResult with details of what changed.
        """
        self._registry.load_all()
        # Route
        decision = await self._route(
            trajectory,
            task_name=task_name,
            reward=reward,
            failed_tests=failed_tests,
            used_skills=used_skills or [],
        )

        log.info(
            "evolve: router decided %d actions for task %r: %s",
            len(decision.actions),
            task_name,
            decision.reasoning,
        )

        # Dispatch
        result = EvolveResult(
            actions_taken=[],
            skills_created=[],
            skills_refined=[],
            skills_deleted=[],
        )

        for action in decision.actions:
            try:
                if action.action == "create":
                    await self._do_create(
                        trajectory, action, task_context, result,
                    )
                elif action.action == "refine":
                    await self._do_refine(trajectory, action, result)
                elif action.action == "delete":
                    self._do_delete(action, result)
            except Exception:
                log.exception(
                    "evolve: failed to execute action %s on %r",
                    action.action, action.skill_name,
                )
                result.actions_taken.append({
                    "action": action.action,
                    "skill": action.skill_name,
                    "status": "error",
                    "reason": action.reason,
                })

        return result

    async def _route(
        self,
        trajectory: RunTrajectory,
        *,
        task_name: str,
        reward: float | None,
        failed_tests: list[str] | None,
        used_skills: list[str],
    ) -> EvolveDecision:
        """Ask LLM to decide what evolution actions to take."""

        traj_lines: list[str] = []
        for step in trajectory.steps:
            if step.instruction:
                traj_lines.append(f"### Task instruction\n{step.instruction}")
            transcript = render_action_transcript(step.actions)
            if transcript:
                traj_lines.append("### Trace\n" + transcript)
            if step.executor_response:
                traj_lines.append(
                    f"### Final response\n{step.executor_response}"
                )
            if step.verify_reason:
                traj_lines.append(f"### Verification\n{step.verify_reason}")

        prompt = _ROUTER_PROMPT.format(
            used_skills=", ".join(used_skills) if used_skills else "(none)",
            task_name=task_name,
            reward=reward if reward is not None else "N/A",
            failed_tests=", ".join(failed_tests[:5]) if failed_tests else "(none)",
            trajectory_summary="\n".join(traj_lines),
        )

        messages: list[dict] = [{"role": "user", "content": prompt}]

        try:
            decision, _ = await self._llm.complete_structured(
                messages,
                response_model=EvolveDecision,
                model=self._model,
                temperature=self._temperature,
            )
            return decision
        except Exception:
            log.exception("evolve: router LLM call failed, returning empty decision")
            return EvolveDecision(actions=[], reasoning="Router LLM call failed")

    async def _do_create(
        self,
        trajectory: RunTrajectory,
        action: EvolveAction,
        task_context: str,
        result: EvolveResult,
    ) -> None:
        """Create a new skill from the trajectory."""
        skill = await self._builder.from_trajectory(
            trajectory,
            self._llm,
            task_context=task_context,
            model=self._model,
            temperature=self._temperature,
            router_analysis=action.reason or "",
        )

        # Use router-suggested name if the LLM generated something generic
        if action.skill_name and action.skill_name != skill.name:
            skill.name = action.skill_name
            skill.normalize_name()

        skill_dir = self._skills_dir / skill.name
        before = _snapshot_skill_dir(skill_dir)
        render_skill(skill, skill_dir)
        after = _snapshot_skill_dir(skill_dir)
        diff = _compute_skill_diff(before, after)
        log.info("evolve: created skill %r at %s", skill.name, skill_dir)

        result.skills_created.append(skill.name)
        result.actions_taken.append({
            "action": "create",
            "skill": skill.name,
            "status": "ok",
            "reason": action.reason,
            "diff": diff,
        })

    async def _do_refine(
        self,
        trajectory: RunTrajectory,
        action: EvolveAction,
        result: EvolveResult,
    ) -> None:
        """Refine an existing skill with trajectory feedback."""
        existing = self._registry.get(action.skill_name)
        if existing is None:
            log.warning(
                "evolve: skill %r not found for refine, skipping",
                action.skill_name,
            )
            result.actions_taken.append({
                "action": "refine",
                "skill": action.skill_name,
                "status": "skipped",
                "reason": "skill not found",
            })
            return

        refined = await self._builder.refine(
            existing,
            trajectory,
            self._llm,
            model=self._model,
            temperature=self._temperature,
            router_analysis=action.reason or "",
        )

        skill_dir = self._skills_dir / refined.name
        before = _snapshot_skill_dir(skill_dir)
        render_skill(refined, skill_dir)
        after = _snapshot_skill_dir(skill_dir)
        diff = _compute_skill_diff(before, after)
        log.info("evolve: refined skill %r", refined.name)

        result.skills_refined.append(refined.name)
        result.actions_taken.append({
            "action": "refine",
            "skill": refined.name,
            "status": "ok",
            "reason": action.reason,
            "diff": diff,
        })

    def _do_delete(
        self,
        action: EvolveAction,
        result: EvolveResult,
    ) -> None:
        """Delete an obsolete skill."""
        skill_dir = self._skills_dir / action.skill_name
        before = _snapshot_skill_dir(skill_dir)
        deleted = self._registry.delete(action.skill_name)
        status = "ok" if deleted else "skipped"
        entry: dict[str, Any] = {
            "action": "delete",
            "skill": action.skill_name,
            "status": status,
            "reason": action.reason,
        }
        if deleted:
            result.skills_deleted.append(action.skill_name)
            after = _snapshot_skill_dir(skill_dir)
            entry["diff"] = _compute_skill_diff(before, after)
        result.actions_taken.append(entry)

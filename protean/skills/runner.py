"""Step runner — execute and validate skill steps.

Two modes of operation:
  - validate:   execute + post-execution verification, autonomous
  - assisted:   execute + verification + assistant correction on failure

For plain replay without verification, use the executor directly
(see TeachSession._run_replay).

Full mode: execute entire skill, verify success_criteria at end.
Step-by-step mode: execute each step as a DAG, verify after each.

Timeout architecture:
  - Per-skill: 300s (SKILL_TIMEOUT_SEC)
  - Per-step verification: 30s (handled by StepVerifier)
  - Retry budget: 3 attempts per idempotent step
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from protean.channels.base import AssistantChannel
from protean.config import ProteanConfig
from protean.executor import ExecutorEvent, attach_assistant_channel
from protean.executor import ExecutorEventType as EET
from protean.skills.renderer import render_skill, render_skill_for_llm
from protean.skills.verifier import StepVerifier, VerifyResult

if TYPE_CHECKING:
    from protean.executor import ExecutorProvider
    from protean.llm import LLM
    from protean.platform.base import Platform
    from protean.skills.builder import SkillBuilder
    from protean.skills.schema import Skill, Step
    from protean.skills.telemetry import TelemetryLogger

log = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────

SKILL_TIMEOUT_SEC = 60000.0   # 1000 minutes per skill execution
STEP_RETRY_BUDGET = 3       # max retries per step

ASSISTANT_RETRY_BUDGET = 3  # max correction attempts via assistant


# ── Execution mode ───────────────────────────────────────


class RunMode(str, Enum):
    VALIDATE = "validate"  # verify after execution, autonomous
    ASSISTED = "assisted"  # verify + ask assistant on failure


class ExecutionMode(str, Enum):
    FULL = "full"              # execute entire skill, verify end-state only
    STEP_BY_STEP = "step_by_step"  # execute step by step, verify each


# ── Result types ─────────────────────────────────────────


class StepResult(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_ASSISTANT = "needs_assistant"


@dataclass
class StepValidation:
    """Validation result for a single step."""

    index: int
    result: StepResult
    attempts: int = 1
    reason: str = ""
    screenshot: bytes | None = None
    strategy_used: str = ""  # which verification strategy produced the result


@dataclass
class ExecutorAction:
    """A single physical action the executor took during a step."""

    tool_name: str = ""       # e.g. "screenshot", "click_at", "find_elements"
    tool_args: dict = field(default_factory=dict)
    result: str = ""          # tool result or text message
    event_type: str = ""      # "tool_call", "tool_result", "message"


@dataclass
class StepTrajectory:
    """Full execution record for a single step (for skill refinement).

    A single logical step may produce many physical actions — activate
    app, find element, click, re-screenshot, etc. We capture all of them.
    """

    step_index: int
    step_name: str
    instruction: str          # text sent to executor
    actions: list[ExecutorAction] = field(default_factory=list)
    executor_response: str = ""  # final DONE/ERROR message
    verify_strategy: str = ""
    verify_passed: bool = False
    verify_reason: str = ""
    resolved_by_assistant: bool = False
    attempts: int = 1
    screenshot: bytes | None = None  # post-verification screenshot


@dataclass
class RunTrajectory:
    """Full execution trajectory for a skill run."""

    skill_name: str
    steps: list[StepTrajectory] = field(default_factory=list)
    duration: float = 0.0
    task: str = ""                # task instruction / description


@dataclass
class RunReport:
    """Result of a skill run with optional validation."""

    skill_name: str
    mode: RunMode
    steps: list[StepValidation] = field(default_factory=list)
    execution_result: str = ""
    duration: float = 0.0
    trajectory: RunTrajectory | None = None

    @property
    def passed(self) -> bool:
        return all(s.result == StepResult.PASSED for s in self.steps)

    @property
    def aborted_at(self) -> int | None:
        for s in self.steps:
            if s.result in (StepResult.FAILED, StepResult.NEEDS_ASSISTANT):
                return s.index
        return None


# ── Step runner ──────────────────────────────────────────


class StepRunner:
    """Execute a skill and optionally validate each step.

    Usage:
        runner = StepRunner(executor, platform, llm)
        report = await runner.run(skill, mode=RunMode.VALIDATE)
    """

    def __init__(
        self,
        executor: ExecutorProvider,
        platform: Platform,
        llm: LLM,
        skill_builder: SkillBuilder | None = None,
        *,
        assistant: AssistantChannel | None = None,
        telemetry: TelemetryLogger | None = None,
        execution_mode: ExecutionMode = ExecutionMode.FULL,
        on_event: Callable[[ExecutorEvent], None] | None = None,
    ) -> None:
        self._executor = executor
        self._platform = platform
        self._llm = llm
        self._skill_builder = skill_builder
        self._assistant = assistant
        self._telemetry = telemetry
        self._execution_mode = execution_mode
        self._executor_started = False
        self._on_event = on_event

        # If the executor exposes set_assistant_channel (e.g. ClaudeCodeExecutor's
        # ask_user MCP bridge), wire the same AssistantChannel through so Claude
        # Code's interactive questions reach the user.
        attach_assistant_channel(executor, assistant)

    def _notify(self, evt: ExecutorEvent) -> None:
        """Forward executor event to the on_event callback if set."""
        if self._on_event:
            self._on_event(evt)

    async def run(
        self,
        skill: Skill,
        *,
        mode: RunMode = RunMode.VALIDATE,
        skill_dir: Path | None = None,
        task: str = "",
    ) -> RunReport:
        """Execute a skill and validate the outcome.

        Args:
            skill: The skill to execute.
            mode: VALIDATE or ASSISTED.
            skill_dir: Optional path to skill directory on disk.
            task: Optional user task description appended to the instruction.
        """
        start = time.monotonic()
        report = RunReport(skill_name=skill.name, mode=mode)

        try:
            with self._platform.keep_awake():
                report = await asyncio.wait_for(
                    self._run_inner(skill, mode=mode, skill_dir=skill_dir, task=task),
                    timeout=SKILL_TIMEOUT_SEC,
                )
        except asyncio.TimeoutError:
            log.error("Skill '%s' timed out after %.0fs", skill.name, SKILL_TIMEOUT_SEC)
            report.steps = [
                StepValidation(
                    index=0, result=StepResult.FAILED,
                    reason=f"Skill execution timed out after {SKILL_TIMEOUT_SEC}s",
                )
            ]
        except Exception:
            log.exception("Skill '%s' execution failed", skill.name)
            report.steps = [
                StepValidation(
                    index=0, result=StepResult.FAILED,
                    reason="Unexpected error during skill execution",
                )
            ]

        report.duration = time.monotonic() - start

        # Log telemetry
        if self._telemetry:
            self._log_telemetry(report)

        # Send completion notification
        try:
            status = "passed" if report.passed else "failed"
            self._platform.notify(
                title=f"Skill '{skill.name}' {status}",
                message=f"Duration: {report.duration:.1f}s",
            )
        except Exception:
            log.debug("Failed to send completion notification", exc_info=True)

        return report

    async def _run_inner(
        self,
        skill: Skill,
        *,
        mode: RunMode,
        skill_dir: Path | None,
        task: str = "",
    ) -> RunReport:
        """Core execution logic (inside the timeout wrapper)."""
        self._task = task
        if self._execution_mode == ExecutionMode.FULL:
            report = await self._run_full(skill, mode=mode, skill_dir=skill_dir)
        else:
            report = await self._run_step_by_step(skill, mode=mode, skill_dir=skill_dir)

        # ── Post-run skill refinement ──
        # Every run feeds back: sharpen actions, tool hints,
        # verify conditions. Does NOT re-run the skill.
        try:
            updated_skill = await self._refine_skill(skill, report)
            if updated_skill:
                report.execution_result += " [skill refined]"
        except Exception:
            log.warning(
                "Skill refinement failed for '%s'",
                skill.name,
                exc_info=True,
            )

        return report

    # ── Full execution + end-state verification ────

    async def _run_full(
        self,
        skill: Skill,
        *,
        mode: RunMode,
        skill_dir: Path | None,
    ) -> RunReport:
        """Execute the entire skill, then verify success_criteria only.

        Per-step verify_conditions are NOT checked in full mode because
        intermediate states may be overwritten by later steps.
        """
        report = RunReport(skill_name=skill.name, mode=mode)

        # Step 1: Execute the full skill
        instruction, result, actions = await self._execute_full(skill, skill_dir)
        report.execution_result = result

        # Step 2: Verify success_criteria (end-state only)
        verify_strategy = ""
        verify_passed = False
        verify_reason = ""
        verify_screenshot: bytes | None = None

        if skill.success_criteria:
            verifier = StepVerifier(self._platform, self._llm, task=self._task)

            # Determine target_app from the last step (most likely the final state app)
            target_app = ""
            if skill.steps:
                target_app = skill.steps[-1].target_app

            outcomes = await verifier.verify_success_criteria(
                skill.success_criteria,
                target_app=target_app,
            )

            all_passed = True
            for i, outcome in enumerate(outcomes):
                passed = outcome.result == VerifyResult.PASSED
                if not passed:
                    all_passed = False

                report.steps.append(StepValidation(
                    index=i,
                    result=StepResult.PASSED if passed else StepResult.FAILED,
                    reason=outcome.reason,
                    screenshot=outcome.screenshot,
                    strategy_used=outcome.strategy_used,
                ))

            verify_passed = all_passed
            # Summarize verification for trajectory
            if outcomes:
                strategies = {o.strategy_used for o in outcomes if o.strategy_used}
                verify_strategy = ", ".join(sorted(strategies)) if strategies else ""
                failed = [o for o in outcomes if o.result != VerifyResult.PASSED]
                if failed:
                    verify_reason = "; ".join(o.reason for o in failed)
                    verify_screenshot = failed[0].screenshot
                else:
                    verify_reason = "All success criteria passed"

            # If any criterion failed, try to fix via executor
            if not all_passed:
                all_passed = await self._fix_failed_criteria(
                    skill, report, verifier, target_app, mode,
                )

        else:
            # No success criteria defined; mark as passed with warning
            log.warning(
                "Skill '%s' has no success_criteria; marking as passed by default",
                skill.name,
            )
            report.steps = [
                StepValidation(
                    index=0,
                    result=StepResult.PASSED,
                    reason="No success_criteria defined",
                    strategy_used="none",
                )
            ]
            verify_passed = True
            verify_reason = "No success_criteria defined"

        # Build trajectory for full execution
        report.trajectory = RunTrajectory(
            skill_name=skill.name,
            steps=[StepTrajectory(
                step_index=-1,
                step_name="full-execution",
                instruction=instruction,
                actions=actions,
                executor_response=result,
                verify_strategy=verify_strategy,
                verify_passed=verify_passed,
                verify_reason=verify_reason,
                screenshot=verify_screenshot,
            )],
        )

        return report

    # ── Step-by-step execution + per-step verification ──

    async def _run_step_by_step(
        self,
        skill: Skill,
        *,
        mode: RunMode,
        skill_dir: Path | None,
    ) -> RunReport:
        """Execute steps as a DAG with per-step verification.

        Default flow is sequential. After each step passes, branch
        conditions are evaluated — first match overrides the next step.

        Idempotent steps are retried up to STEP_RETRY_BUDGET times.
        Non-idempotent steps execute once (gate checks pre-conditions).
        Failed steps are recorded and execution continues.
        In assisted mode, failed steps are offered to the assistant first.
        """
        report = RunReport(skill_name=skill.name, mode=mode)
        trajectory_steps: list[StepTrajectory] = []

        verifier = StepVerifier(self._platform, self._llm, task=self._task)

        step_map: dict[str, int] = {
            s.name: idx for idx, s in enumerate(skill.steps) if s.name
        }

        current = 0
        prev_index = -1
        max_visits = len(skill.steps) * 3

        for _ in range(max_visits):
            if current >= len(skill.steps):
                break

            i = current
            step = skill.steps[i]

            log.info(
                "Step %d/%d: %s", i + 1, len(skill.steps), step.name,
            )
            self._notify(ExecutorEvent(
                type=EET.ITERATION,
                message=f"Step {i + 1}/{len(skill.steps)}: {step.name}",
            ))

            # ── Idempotent gate ──────────────────────────────
            # Non-idempotent step: check pre-conditions before attempting.
            # Gate failure → abort (we can't proceed without this action,
            # and we refuse to attempt it in an uncertain state).
            if not step.idempotent:
                gate_passed = await self._idempotent_gate(
                    skill, step, i, prev_index, mode, verifier,
                )
                if not gate_passed:
                    report.steps.append(StepValidation(
                        index=i,
                        result=StepResult.FAILED,
                        reason="Idempotent gate failed; aborting",
                    ))
                    trajectory_steps.append(StepTrajectory(
                        step_index=i,
                        step_name=step.name or f"step-{i}",
                        instruction="(not executed — idempotent gate failed)",
                        verify_passed=False,
                        verify_reason="Idempotent gate failed; aborting",
                    ))
                    break

            # ── Execute + verify ─────────────────────────────
            step_passed = False
            attempts = 0
            last_reason = ""
            last_strategy = ""
            last_screenshot = None
            last_instruction = ""
            last_executor_response = ""
            all_actions: list[ExecutorAction] = []
            budget = STEP_RETRY_BUDGET if step.idempotent else 1

            for attempt in range(budget):
                attempts = attempt + 1
                retry_reason = last_reason if attempt > 0 else None
                last_instruction, step_actions, last_executor_response = (
                    await self._execute_single_step(
                        skill, step, i, skill_dir,
                        retry_reason=retry_reason,
                    )
                )
                all_actions.extend(step_actions)
                outcome = await verifier.verify_step(step)
                last_reason = outcome.reason
                last_strategy = outcome.strategy_used
                last_screenshot = outcome.screenshot

                if outcome.result == VerifyResult.PASSED:
                    step_passed = True
                    break

                log.info(
                    "Step %d '%s' attempt %d/%d failed: %s",
                    i, step.name, attempts, budget, outcome.reason,
                )

            # ── Handle result ────────────────────────────────
            if step_passed:
                report.steps.append(StepValidation(
                    index=i,
                    result=StepResult.PASSED,
                    attempts=attempts,
                    reason=last_reason,
                    strategy_used=last_strategy,
                ))
            elif mode == RunMode.ASSISTED and self._assistant:
                resolved = await self._ask_assistant(
                    skill, step, i, last_reason, last_screenshot,
                )
                if resolved:
                    report.steps.append(StepValidation(
                        index=i,
                        result=StepResult.PASSED,
                        attempts=attempts,
                        reason="Resolved by assistant",
                        strategy_used="assistant",
                    ))
                    step_passed = True
                else:
                    report.steps.append(StepValidation(
                        index=i,
                        result=StepResult.NEEDS_ASSISTANT,
                        attempts=attempts,
                        reason=last_reason,
                        strategy_used=last_strategy,
                        screenshot=last_screenshot,
                    ))
            else:
                report.steps.append(StepValidation(
                    index=i,
                    result=StepResult.FAILED,
                    attempts=attempts,
                    reason=last_reason,
                    strategy_used=last_strategy,
                    screenshot=last_screenshot,
                ))

            # ── Record trajectory ────────────────────────────
            # Use the final outcome — if assistant resolved it,
            # note that in trajectory so refinement LLM knows.
            assistant_resolved = bool(
                step_passed
                and report.steps
                and report.steps[-1].strategy_used == "assistant"
            )
            trajectory_steps.append(StepTrajectory(
                step_index=i,
                step_name=step.name or f"step-{i}",
                instruction=last_instruction,
                actions=all_actions,
                executor_response=last_executor_response,
                verify_strategy=last_strategy,
                verify_passed=step_passed,
                verify_reason=last_reason,
                resolved_by_assistant=assistant_resolved,
                attempts=attempts,
                screenshot=last_screenshot,
            ))

            # ── Advance ──────────────────────────────────────
            prev_index = i
            if step_passed:
                next_name = await self._resolve_branches(
                    step, verifier,
                )
                if next_name and next_name in step_map:
                    target = step_map[next_name]
                    if target != current + 1:
                        skipped = [
                            skill.steps[j].name or f"step-{j}"
                            for j in range(current + 1, min(target, len(skill.steps)))
                        ]
                        if skipped:
                            log.info(
                                "Branch jump: %s → %s (skipping %s)",
                                step.name, next_name, ", ".join(skipped),
                            )
                    current = target
                else:
                    current += 1
            else:
                current += 1

        report.trajectory = RunTrajectory(skill_name=skill.name, steps=trajectory_steps)

        # ── Final success_criteria check ─────────────────
        # After all steps, verify overall success_criteria (same as full mode)
        if skill.success_criteria:
            target_app = ""
            if skill.steps:
                target_app = skill.steps[-1].target_app

            outcomes = await verifier.verify_success_criteria(
                skill.success_criteria,
                target_app=target_app,
            )

            all_passed = all(o.result == VerifyResult.PASSED for o in outcomes)
            if not all_passed:
                for i, outcome in enumerate(outcomes):
                    if outcome.result != VerifyResult.PASSED:
                        report.steps.append(StepValidation(
                            index=len(skill.steps) + i,
                            result=StepResult.FAILED,
                            reason=f"success_criteria: {outcome.reason}",
                            screenshot=outcome.screenshot,
                            strategy_used=outcome.strategy_used,
                        ))

        return report

    # ── Execution helpers ────────────────────────────────────

    async def _execute_full(
        self, skill: Skill, skill_dir: Path | None,
    ) -> tuple[str, str, list[ExecutorAction]]:
        """Execute the full skill via executor.

        Returns (instruction, result_summary, actions).
        """
        skill_blocks = render_skill_for_llm(skill)
        # Resolve ${SKILL_DIR} to absolute path so run_terminal_command works
        if skill_dir:
            skill_blocks = self._resolve_skill_dir(skill_blocks, skill_dir)
        instruction = self._build_instruction(skill)

        await self._executor.start_task(instruction, content_blocks=skill_blocks or None)

        done_msg = ""
        all_messages: list[str] = []
        actions: list[ExecutorAction] = []

        async for evt in self._executor.get_events():
            self._notify(evt)
            if evt.type == EET.TOOL_CALL:
                log.info("Executor tool: %s", evt.tool_name)
                actions.append(ExecutorAction(
                    tool_name=evt.tool_name,
                    tool_args=evt.tool_args,
                    event_type="tool_call",
                ))
            elif evt.type == EET.TOOL_RESULT:
                actions.append(ExecutorAction(
                    tool_name=evt.tool_name,
                    result=evt.result,
                    event_type="tool_result",
                ))
            elif evt.type == EET.MESSAGE:
                msg = evt.message or evt.reasoning or ""
                all_messages.append(msg)
                actions.append(ExecutorAction(
                    result=msg,
                    event_type="message",
                ))
            elif evt.type == EET.DONE:
                done_msg = evt.message
                break
            elif evt.type == EET.ERROR:
                done_msg = evt.error or evt.message or "Unknown error"
                break

        result = done_msg or (all_messages[-1] if all_messages else "Done")
        return instruction, result, actions

    async def _execute_single_step(
        self,
        skill: Skill,
        step: Step,
        step_index: int,
        skill_dir: Path | None,
        *,
        retry_reason: str | None = None,
    ) -> tuple[str, list[ExecutorAction], str]:
        """Execute a single step by sending it to the executor.

        Uses start_task for the first step (to establish the session) and
        send_message for subsequent steps (to maintain inter-step context).

        Returns (instruction, actions, final_response).
        """
        instruction = (
            f"Execute ONLY step {step_index + 1} of skill '{skill.name}':\n"
            f"Step: {step.name}\n"
            f"Action: {step.action}\n"
            f"FIRST attempt the exact method described above (tool hint, "
            f"target app, GUI path). Only try an alternative if that method "
            f"fails or does not apply to the current screen state.\n"
            f"If the step is complete, call `done`. "
            f"Do NOT proceed to subsequent steps."
        )
        if step.tool:
            instruction += f"Tool chain: {step.tool}\n"
        if step.target_app:
            instruction += f"Target app: {step.target_app}\n"
        if retry_reason:
            instruction += (
                f"\nIMPORTANT: The previous attempt at this step FAILED "
                f"verification: {retry_reason}\n"
                f"You MUST re-execute this step. Take a screenshot first "
                f"to see the current screen state, then perform the action."
            )

        if not self._executor_started:
            skill_blocks = render_skill_for_llm(skill)
            if skill_dir:
                skill_blocks = self._resolve_skill_dir(skill_blocks, skill_dir)
            await self._executor.start_task(
                instruction, content_blocks=skill_blocks or None,
            )
            self._executor_started = True
        else:
            await self._executor.send_message(instruction)

        actions: list[ExecutorAction] = []
        done_msg = ""
        async for evt in self._executor.get_events():
            self._notify(evt)
            if evt.type == EET.TOOL_CALL:
                actions.append(ExecutorAction(
                    tool_name=evt.tool_name,
                    tool_args=evt.tool_args,
                    event_type="tool_call",
                ))
            elif evt.type == EET.TOOL_RESULT:
                actions.append(ExecutorAction(
                    tool_name=evt.tool_name,
                    result=evt.result,
                    event_type="tool_result",
                ))
            elif evt.type == EET.MESSAGE:
                msg = evt.message or evt.reasoning or ""
                actions.append(ExecutorAction(
                    result=msg,
                    event_type="message",
                ))
            elif evt.type == EET.DONE:
                done_msg = evt.message
                break
            elif evt.type == EET.ERROR:
                done_msg = evt.error or evt.message or "Step error"
                break

        return instruction, actions, done_msg

    # ── Branch resolution ────────────────────────────────

    async def _resolve_branches(
        self,
        step: "Step",
        verifier: "StepVerifier",
    ) -> str | None:
        """Evaluate branch conditions after a step passes.

        Takes a screenshot and asks the LLM whether each condition holds.
        Returns the next_step name of the first matching branch, or None
        if no branch matches (continue sequentially).
        """
        if not step.branches:
            return None

        for branch in step.branches:
            outcome = await verifier.verify_condition(
                branch.condition, target_app=step.target_app,
            )
            if outcome.result == VerifyResult.PASSED:
                return branch.next_step

        return None

    # ── Idempotent gate ────────────────────────────────────

    async def _idempotent_gate(
        self,
        skill: "Skill",
        step: "Step",
        step_index: int,
        prev_index: int,
        mode: RunMode,
        verifier: "StepVerifier",
    ) -> bool:
        """Gate check before executing a non-idempotent step.

        Returns True to proceed, False to abort.

        - Assisted mode: ask the assistant for confirmation.
        - Validate mode: verify the previous step's condition passed
          (we're in the expected state before doing something irreversible).
          If no previous step or no verify_condition, proceed.
        """
        if mode == RunMode.ASSISTED and self._assistant:
            answer = await self._assistant.ask(
                f"Irreversible step — proceed?\n"
                f"Step {step_index + 1}: {step.name}\n"
                f"Action: {step.action}\n\n"
                f"Reply 'yes' to execute, 'skip' to skip.",
            )
            return answer.strip().lower() not in ("skip", "no", "n")

        # Validate mode: verify prior state is correct before proceeding.
        # prev_index tracks the actual step we came from (not step_index - 1,
        # which could be wrong after a branch jump).
        if prev_index >= 0:
            prev_step = skill.steps[prev_index]
            if prev_step.verify_condition:
                outcome = await verifier.verify_step(prev_step)
                if outcome.result != VerifyResult.PASSED:
                    log.warning(
                        "Idempotent gate: previous step '%s' verification "
                        "failed (%s); skipping non-idempotent step '%s'",
                        prev_step.name, outcome.reason, step.name,
                    )
                    return False

        return True

    def _build_instruction(self, skill: Skill) -> str:
        lines = [
            f"Execute the skill '{skill.name}' end-to-end.",
            "The rendered SKILL.md above is your PRIMARY execution plan. "
            "Follow the steps, tools, and methods described in it faithfully.",
            "Execute the whole workflow without waiting for step-by-step prompts.",
            "IMPORTANT: For each step, FIRST attempt the method described in "
            "SKILL.md (GUI path, tool hint, target app). Only if that method "
            "fails or is clearly inapplicable to the current screen state "
            "should you try an alternative approach.",
            "If the current UI differs slightly, adapt naturally while preserving the goal.",
            "The figures above are REFERENCE images from the original "
            "demonstration, NOT live screenshots.",
            "Always take a screenshot first to see the actual current screen state before acting.",
            "Terminate ONLY when the final goal is achieved.",
        ]
        if self._task:
            lines.append(f"User task: {self._task}")
        return "\n".join(lines)

    @staticmethod
    def _resolve_skill_dir(
        blocks: list[str | tuple[bytes, str]], skill_dir: Path,
    ) -> list[str | tuple[bytes, str]]:
        """Replace ${SKILL_DIR} with the absolute skill directory path."""
        abs_dir = str(skill_dir.resolve())
        resolved: list[str | tuple[bytes, str]] = []
        for block in blocks:
            if isinstance(block, str):
                resolved.append(block.replace("${SKILL_DIR}", abs_dir))
            else:
                resolved.append(block)
        return resolved

    # ── Criteria fix (full mode) ─────────────────────────────

    async def _fix_failed_criteria(
        self,
        skill: Skill,
        report: RunReport,
        verifier: StepVerifier,
        target_app: str,
        mode: RunMode,
    ) -> bool:
        """Try to fix failed success_criteria via executor.

        ASSISTED mode: ask assistant for fix instruction, send to executor.
        VALIDATE mode: send failed criteria directly to executor.
        Both retry up to ASSISTANT_RETRY_BUDGET times.

        Updates report.steps in-place. Returns True if all criteria pass.
        """
        for attempt in range(ASSISTANT_RETRY_BUDGET):
            failed_criteria = [
                (i, sv) for i, sv in enumerate(report.steps)
                if sv.result != StepResult.PASSED
            ]
            if not failed_criteria:
                return True

            failure_summary = "\n".join(
                f"  - Criterion {i + 1}: {sv.reason}"
                for i, sv in failed_criteria
            )

            if mode == RunMode.ASSISTED and self._assistant:
                # Option A: ask assistant what to do
                correction = await self._assistant.ask(
                    f"Success criteria failed ({attempt + 1}/{ASSISTANT_RETRY_BUDGET})\n"
                    f"Skill: {skill.name}\n"
                    f"{failure_summary}\n\n"
                    f"What should the agent do to fix this?"
                )
                if not correction.strip():
                    return False
                fix_instruction = correction
            else:
                # Option B: tell executor directly
                fix_instruction = (
                    f"The following success criteria for skill '{skill.name}' "
                    f"are not yet met:\n{failure_summary}\n\n"
                    f"Take corrective action to satisfy these criteria."
                )

            # Send to executor
            if self._executor_started:
                await self._executor.send_message(fix_instruction)
            else:
                await self._executor.start_task(fix_instruction)
                self._executor_started = True

            async for evt in self._executor.get_events():
                self._notify(evt)
                if evt.type in (EET.DONE, EET.ERROR):
                    break

            # Re-verify all criteria
            re_outcomes = await verifier.verify_success_criteria(
                skill.success_criteria, target_app=target_app,
            )
            for i, sv in failed_criteria:
                if i < len(re_outcomes):
                    if re_outcomes[i].result == VerifyResult.PASSED:
                        sv.result = StepResult.PASSED
                        sv.reason = "Fixed by executor"
                    else:
                        sv.reason = re_outcomes[i].reason

            if all(sv.result == StepResult.PASSED for _, sv in enumerate(report.steps)):
                return True

            log.info(
                "Criteria fix attempt %d/%d failed",
                attempt + 1, ASSISTANT_RETRY_BUDGET,
            )

        return False

    # ── Assistant escalation ─────────────────────────────────

    async def _ask_assistant(
        self,
        skill: Skill,
        step: Step,
        step_index: int,
        failure_reason: str,
        screenshot: bytes | None,
    ) -> bool:
        """Ask the assistant how to fix a failed step, up to 3 attempts.

        Each attempt:
          1. Ask assistant for a correction instruction.
          2. Send it to executor for re-execution.
          3. Re-verify.
          4. If still fails, loop with updated failure reason.

        Records corrections in skill_builder (if present) for refinement.
        Returns True if the step eventually passes verification.
        """
        if not self._assistant:
            return False

        verifier = StepVerifier(self._platform, self._llm, task=self._task)
        reason = failure_reason

        for attempt in range(ASSISTANT_RETRY_BUDGET):
            question = (
                f"Step {step_index + 1} failed ({attempt + 1}/{ASSISTANT_RETRY_BUDGET})\n"
                f"Step: {step.name}\n"
                f"Action: {step.action}\n"
                f"Reason: {reason}\n\n"
                f"How should the agent fix this?"
            )

            correction = await self._assistant.ask(
                question, screenshot if attempt == 0 else None,
            )
            if not correction.strip():
                return False

            # Record in skill builder for later refinement
            if self._skill_builder is not None:
                self._skill_builder.add_observed_step(
                    intent=step.name,
                    action=correction.strip(),
                )

            # Send correction to executor
            correction_instruction = (
                f"Correction for step {step_index + 1} '{step.name}':\n"
                f"{correction}\n"
            )
            if step.target_app:
                correction_instruction += f"Target app: {step.target_app}\n"

            if self._executor_started:
                await self._executor.send_message(correction_instruction)
            else:
                await self._executor.start_task(correction_instruction)
                self._executor_started = True

            # Drain executor events
            async for evt in self._executor.get_events():
                self._notify(evt)
                if evt.type in (EET.DONE, EET.ERROR):
                    break

            # Re-verify
            outcome = await verifier.verify_step(step)
            if outcome.result == VerifyResult.PASSED:
                return True

            reason = outcome.reason
            screenshot = outcome.screenshot
            log.info(
                "Assistant correction attempt %d/%d for step %d failed: %s",
                attempt + 1, ASSISTANT_RETRY_BUDGET, step_index, reason,
            )

        return False

    # ── Telemetry ────────────────────────────────────────────

    def _log_telemetry(self, report: RunReport) -> None:
        """Log the run report to the telemetry system."""
        if not self._telemetry:
            return

        try:
            from protean.skills.telemetry import StepTelemetry as ST

            step_tel = [
                ST(
                    step_index=sv.index,
                    verify_result=sv.result.value,
                    strategy_used=sv.strategy_used,
                    attempts=sv.attempts,
                )
                for sv in report.steps
            ]
            entry = self._telemetry.from_report(
                report,
                execution_mode=self._execution_mode.value,
                step_telemetry=step_tel,
            )
            self._telemetry.log(entry)
        except Exception:
            log.warning("Failed to log telemetry", exc_info=True)

    # ── Skill refinement ─────────────────────────────────────

    async def _refine_skill(
        self,
        original_skill: Skill,
        report: RunReport,
    ) -> Skill | None:
        """Re-finalize the skill after a run.

        Uses the builder to incorporate execution feedback (corrections,
        verification outcomes) and produce a refined skill. Saves to disk.
        Does NOT re-run the skill — the caller controls the loop.

        Returns the updated Skill, or None on failure.
        """
        if not self._skill_builder:
            log.debug("Skipping refinement: no skill_builder provided")
            return None

        if not report.trajectory or not report.trajectory.steps:
            log.info(
                "Skipping refinement for '%s': no trajectory available",
                original_skill.name,
            )
            return None

        log.info("Refining skill '%s' from trajectory", original_skill.name)
        updated_skill = await self._skill_builder.refine(
            original_skill, report.trajectory, self._llm,
        )

        config = ProteanConfig.load()
        skill_dir = config.skills_dir / original_skill.name
        render_skill(updated_skill, skill_dir)
        log.info(
            "Refined skill '%s' saved to %s",
            updated_skill.name, skill_dir,
        )
        return updated_skill

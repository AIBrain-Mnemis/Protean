"""Skill builder — all skill construction goes through here.

Two paths, one output (Skill object):
  - Realtime (instance methods): step-by-step during voice conversation.
    add_observed_step / add_executed_step / revise / finalize
  - Recording (static methods): batch from offline recording.
    from_evidence / from_recording / from_recording_and_save

These are fundamentally different processes (streaming vs batch) that
share an output type but NOT a construction pipeline.
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError

from protean.llm import ContextOverflowError
from protean.llm.context import derive_image_budget, explicit_image_budget, max_context
from protean.platform.base import prepare_screenshot_for_llm
from protean.skills.prompts import (
    CREATE_FROM_TRAJECTORY_PROMPT,
    FINALIZE_PROMPT,
    OUTPUT_HOTSPOT_HINT_PROMPT,
    RECORDING_SYSTEM_PROMPT,
    REFINE_PROMPT,
)
from protean.skills.schema import Skill, SkillParameter, Step, to_kebab

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from protean.analyzer.evidence import EvidencePack
    from protean.llm import LLM
    from protean.skills.runner import ExecutorAction, RunTrajectory


@dataclass
class RawStep:
    """A step captured during the conversation, before LLM polish."""

    timestamp: float
    source: str  # "observed" | "executed" | "skill"
    intent: str  # semantic goal captured during teaching
    action: str  # what happened or what to do
    tool: str = ""  # optional tool string or tool chain
    result: str = ""  # execution result (if executed)
    screenshots: list[tuple[bytes, float]] = field(default_factory=list)
    feedback: str = ""  # user feedback/correction on this step


class SkillMetadata(BaseModel):
    """LLM-generated metadata for a skill. Steps are NOT included."""

    name: str = Field(description="Kebab-case skill name, max 5 words")
    description: str = Field(description="One-line: what + when to use")
    goal: str = Field(description="One sentence: overall goal")
    when_to_use: list[str] = Field(default_factory=list)
    when_not_to_use: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    parameters: list[SkillParameter] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class FinalizedSkillOutput(SkillMetadata):
    """LLM output for realtime finalize, including rewritten steps."""

    steps: list[Step] = Field(default_factory=list)


def _ht(text: str, head: int, tail: int) -> str:
    """Keep first ``head`` and last ``tail`` chars, dropping the middle."""
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}…[{len(text) - head - tail} chars]…{text[-tail:]}"


def _figure_ref_key(ref: str) -> str:
    raw = ref.strip().replace("\\", "/")
    if raw.startswith("figs/"):
        raw = raw.removeprefix("figs/")
    return raw.lower().replace(" ", "_")


def _resolve_referenced_figures(skill: Skill, available: dict[str, bytes]) -> None:
    lookup: dict[str, tuple[str, bytes]] = {}
    for ref, data in available.items():
        key = _figure_ref_key(ref)
        base = key[:-4] if key.endswith(".jpg") else key
        filename = key if key.endswith(".jpg") else f"{key}.jpg"
        lookup[key] = (filename, data)
        lookup[base] = (filename, data)
        lookup[filename] = (filename, data)

    used: dict[str, bytes] = {}
    for step in skill.steps:
        resolved = []
        for fig in step.figures:
            hit = lookup.get(_figure_ref_key(fig.ref))
            if hit is None:
                continue
            filename, data = hit
            fig.ref = filename
            resolved.append(fig)
            used[filename] = data
        step.figures = resolved
    skill.figure_data = used


# ── Action rendering + cost accounting ────────────────────
# Per-action formatting is shared by render_action_transcript() (transcript
# text sent to the LLM) and _measure_action_costs() (char-cost accounting
# used by render_output_hotspot_hint() to flag heavy outputs in the prompt).
# Both flow through _trace_action() so the numbers and the text can't drift.


@dataclass(frozen=True)
class _ActionCost:
    """Transcript line + char cost for one rendered ExecutorAction.

    ``step_index`` / ``step_name`` are filled in by the trajectory walker
    in ``_measure_action_costs``; left at defaults when used solo from
    ``render_action_transcript``.
    """
    line: str
    event_type: str        # "tool_call" | "tool_result" | "message"
    tool_name: str         # producing tool's name ("" for messages)
    raw_chars: int         # untruncated source-text length
    source_hint: str       # short factual tag (command preview, "agent message")
    step_index: int = -1
    step_name: str = ""


def _tool_call_arg_text(a: ExecutorAction) -> str:
    """Arg string for a tool_call: ``command`` field if present, else ``repr(tool_args)``."""
    cmd = a.tool_args.get("command") if isinstance(a.tool_args, dict) else None
    return cmd if isinstance(cmd, str) and cmd else repr(a.tool_args)


def _trace_action(
    a: ExecutorAction, last_tool: ExecutorAction | None,
) -> tuple[_ActionCost | None, ExecutorAction | None]:
    """Render one action + measure its cost. Single source of truth for both.

    Returns ``(cost, new_last_tool)``. ``cost`` is ``None`` for empty
    tool_result / message (dropped from the transcript). ``new_last_tool``
    is threaded so a tool_result can name its producing tool_call.
    """
    if a.event_type == "tool_call":
        arg = _tool_call_arg_text(a)
        preview = arg.replace("\n", " ⏎ ")[:160]
        return _ActionCost(
            line=f"[tool_call] {a.tool_name}  {_ht(arg, 600, 200)!r}",
            event_type="tool_call", tool_name=a.tool_name,
            raw_chars=len(arg), source_hint=f"call args: {preview!r}",
        ), a
    if a.event_type == "tool_result":
        text = (a.result or "").strip()
        if not text:
            return None, last_tool
        if last_tool is not None:
            producer = last_tool.tool_name
            cmd = _tool_call_arg_text(last_tool).replace("\n", " ⏎ ")[:160]
            hint = f"output of {producer}({cmd!r})"
        else:
            producer = ""
            hint = "tool output (no preceding tool_call recorded)"
        return _ActionCost(
            line=f"[tool_result] len={len(text)}  {_ht(text, 800, 500)!r}",
            event_type="tool_result", tool_name=producer,
            raw_chars=len(text), source_hint=hint,
        ), last_tool
    if a.event_type == "message":
        text = (a.result or "").strip()
        if not text:
            return None, last_tool
        return _ActionCost(
            line=f"[message] {text[:300]!r}",
            event_type="message", tool_name="",
            raw_chars=len(text), source_hint="agent message",
        ), last_tool
    return None, last_tool


def render_action_transcript(actions: list[ExecutorAction]) -> str:
    """One line per action, tagged by event_type."""
    if not actions:
        return "(no actions recorded)"
    lines: list[str] = []
    last_tool: ExecutorAction | None = None
    for a in actions:
        cost, last_tool = _trace_action(a, last_tool)
        if cost is not None:
            lines.append(cost.line)
    return "\n".join(lines)


# TODO: Let skill evolution discover safe shortcuts that merge or replace taught steps.
def _measure_action_costs(trajectory: RunTrajectory) -> list[_ActionCost]:
    """Per-action cost across a trajectory, with step context attached."""
    out: list[_ActionCost] = []
    for t in trajectory.steps:
        last_tool: ExecutorAction | None = None
        for a in t.actions:
            cost, last_tool = _trace_action(a, last_tool)
            if cost is not None:
                out.append(replace(
                    cost, step_index=t.step_index, step_name=t.step_name,
                ))
    return out


def render_output_hotspot_hint(
    costs: list[_ActionCost],
    *,
    top_steps: int = 5,
    min_step_raw_chars: int = 40_000,
) -> str:
    """Advisory prompt block flagging the transcript's largest outputs.

    Returns "" when no step is heavy enough to be worth flagging. The
    default ``min_step_raw_chars`` of 40_000 (~10k tokens at ~4 chars/tok)
    keeps the floor in genuinely-fat-output territory — multiple
    heavy tool_results or one truly large dump per step — so the
    advisory only fires when shaping the skill's output yields real
    savings on every future run.
    """
    if not costs:
        return ""

    step_totals: dict[tuple[int, str], dict[str, int]] = {}
    for c in costs:
        e = step_totals.setdefault(
            (c.step_index, c.step_name),
            {"raw": 0, "rendered": 0, "tool_result_raw": 0},
        )
        e["raw"] += c.raw_chars
        e["rendered"] += len(c.line)
        if c.event_type == "tool_result":
            e["tool_result_raw"] += c.raw_chars

    ranked = sorted(step_totals.items(), key=lambda kv: kv[1]["raw"], reverse=True)
    if not ranked or ranked[0][1]["raw"] < min_step_raw_chars:
        return ""

    total_raw = sum(e["raw"] for e in step_totals.values())
    total_rendered = sum(e["rendered"] for e in step_totals.values())

    step_block = "\n".join(
        f"- Step {idx + 1} \"{name}\": ~{e['raw']:,} raw chars "
        f"(~{e['tool_result_raw']:,} from tool_result; "
        f"~{max(0, e['raw'] - e['rendered']):,} dropped by head+tail compaction)."
        for (idx, name), e in ranked[:top_steps]
    )

    return (
        OUTPUT_HOTSPOT_HINT_PROMPT
        .replace("{total_raw}", f"{total_raw:,}")
        .replace("{total_rendered}", f"{total_rendered:,}")
        .replace("{step_block}", step_block)
    )


# TODO: Let skill evolution discover safe shortcuts that merge or replace taught steps.


class SkillBuilder:
    """Builds a Skill incrementally during a teaching conversation."""

    def __init__(self) -> None:
        self._steps: list[RawStep] = []
        self._start_time = time.monotonic()

    @property
    def step_count(self) -> int:
        return len(self._steps)

    @property
    def steps(self) -> list[RawStep]:
        return list(self._steps)

    def add_observed_step(
        self,
        intent: str,
        action: str,
        screenshots: list[tuple[bytes, float]] | None = None,
    ) -> int:
        """Record a step the user demonstrated. Returns step index."""
        step = RawStep(
            timestamp=time.monotonic() - self._start_time,
            source="observed",
            intent=intent,
            action=action,
            screenshots=screenshots or [],
        )
        self._steps.append(step)
        return len(self._steps) - 1

    def add_executed_step(
        self,
        intent: str,
        action: str,
        tool: str = "",
        result: str = "",
        screenshots: list[tuple[bytes, float]] | None = None,
    ) -> int:
        """Record a step the agent executed. Returns step index."""
        step = RawStep(
            timestamp=time.monotonic() - self._start_time,
            source="executed",
            intent=intent,
            action=action,
            tool=tool,
            result=result,
            screenshots=screenshots or [],
        )
        self._steps.append(step)
        return len(self._steps) - 1

    def revise_step(self, index: int, **updates: str) -> None:
        """Revise a step by index. Accepts intent, action, and feedback."""
        if not 0 <= index < len(self._steps):
            return
        step = self._steps[index]
        if "intent" in updates:
            step.intent = updates["intent"]
        if "action" in updates:
            step.action = updates["action"]
        if "feedback" in updates:
            step.feedback = updates["feedback"]

    def remove_step(self, index: int) -> None:
        """Remove a step by index."""
        if 0 <= index < len(self._steps):
            self._steps.pop(index)

    def insert_step(
        self,
        index: int,
        intent: str,
        action: str,
        source: str = "observed",
    ) -> None:
        """Insert a step at a specific position."""
        step = RawStep(
            timestamp=time.monotonic() - self._start_time,
            source=source,
            intent=intent,
            action=action,
        )
        self._steps.insert(index, step)

    def to_steps(self) -> list[Step]:
        """Convert raw steps to Skill Step objects."""
        return [
            Step(
                name=to_kebab(s.intent) if s.intent else to_kebab(s.action),
                action=s.action,
                tool=s.tool if s.tool else "",
            )
            for s in self._steps
        ]

    def load_steps(self, steps: list[Step], *, source: str = "skill") -> None:
        """Replace the current draft with steps loaded from an existing skill."""
        self._start_time = time.monotonic()
        self._steps = [
            RawStep(
                timestamp=0.0,
                source=source,
                intent=step.name,
                action=step.action,
                tool=step.tool,
            )
            for step in steps
        ]

    def record_step_execution(
        self,
        index: int,
        *,
        result: str,
        screenshots: list[tuple[bytes, float]] | None = None,
    ) -> None:
        """Attach execution artifacts to an existing draft step."""
        if not 0 <= index < len(self._steps):
            return
        step = self._steps[index]
        step.result = result
        step.screenshots = screenshots or []

    async def finalize(self, llm: LLM, *, model: str | None = None,
                       temperature: float = 1.0) -> Skill:
        """Generate final Skill metadata and rewritten steps via LLM."""
        step_lines = []
        for i, s in enumerate(self._steps):
            line = f"{i + 1}. [{s.source}] {s.action}"
            if s.intent:
                line += f" | current_intent={s.intent}"
            if s.tool:
                line += f" | current_tool={s.tool}"
            if s.result:
                line += f" | result={s.result}"
            if s.feedback:
                line += f" (user feedback: {s.feedback})"
            step_lines.append(line)

        prompt = FINALIZE_PROMPT + "\n".join(step_lines)
        messages: list[dict] = [{"role": "user", "content": prompt}]

        skill: Skill | None = None
        max_attempts = 3
        for attempt in range(max_attempts):
            finalized, _ = await llm.complete_structured(
                messages,
                response_model=FinalizedSkillOutput,
                model=model,
                temperature=temperature,
            )

            skill = Skill(
                name=finalized.name,
                description=finalized.description,
                goal=finalized.goal,
                when_to_use=finalized.when_to_use,
                when_not_to_use=finalized.when_not_to_use,
                steps=finalized.steps,
                success_criteria=finalized.success_criteria,
                parameters=finalized.parameters,
                tags=finalized.tags,
                source="taught",
            )
            errors = skill.validate_steps()
            if not errors:
                break
            log.warning(
                "LLM produced invalid steps (attempt %d/%d): %s",
                attempt + 1, max_attempts, errors,
            )
            # Feed errors back so the LLM can fix them
            messages.append({"role": "assistant", "content": finalized.model_dump_json()})
            messages.append({"role": "user", "content": (
                f"The output has step validation errors: {'; '.join(errors)}. "
                "Fix the step names to be unique and ensure all branch "
                "next_step values reference existing step names."
            )})

        assert skill is not None  # at least one attempt always runs
        return skill

    async def _generate_from_trajectory(
        self,
        trajectory: RunTrajectory,
        llm: LLM,
        *,
        existing_skill: Skill | None = None,
        required_name: str = "",
        prompt: str,
        model: str | None = None,
        temperature: float = 1.0,
    ) -> Skill:
        """Shared core for generating or refining a skill from a trajectory.

        When existing_skill is provided, produces a refined version and
        enforces that all original steps are preserved.
        When existing_skill is None, creates a new skill from scratch.
        """
        original_figure_data: dict[str, bytes] = {}

        if existing_skill is not None:
            original_figure_data = dict(existing_skill.figure_data)

        # Collect all action images across the trajectory for budget-aware selection.
        # Each entry: (step_index, frame_number, image_data, mime, role)
        all_action_images: list[tuple[int, int, bytes, str, str]] = []
        frame_number = 0
        for t in trajectory.steps:
            for a in t.actions:
                if a.event_type == "tool_result" and a.images:
                    for img_data, mime, role in a.images:
                        if role == "detail":
                            frame_no = frame_number
                        else:
                            frame_number += 1
                            frame_no = frame_number
                        if frame_no > 0:
                            all_action_images.append((t.step_index, frame_no, img_data, mime, role))

        # Derive image budget and select within it.
        estimated_text_chars = sum(
            len(render_action_transcript(t.actions)) + 200
            for t in trajectory.steps
        )
        explicit = explicit_image_budget()
        if explicit is not None:
            img_budget = explicit
        else:
            img_budget = derive_image_budget(estimated_text_chars, max_context())

        hotspot_hint = render_output_hotspot_hint(
            _measure_action_costs(trajectory),
        )

        def _select_image_indices(image_budget: int) -> set[int]:
            if image_budget <= 0 or not all_action_images:
                return set()
            if len(all_action_images) <= image_budget:
                return set(range(len(all_action_images)))
            n = len(all_action_images)
            selected = {0, n - 1}
            if image_budget > 2:
                step = (n - 1) / (image_budget - 1)
                for i in range(1, image_budget - 1):
                    selected.add(round(i * step))
            return selected

        def _display_ref(filename: str) -> str:
            return filename[:-4] if filename.endswith(".jpg") else filename

        def _build_messages(image_budget: int) -> tuple[list[dict], dict[str, bytes], int]:
            selected_indices = _select_image_indices(image_budget)
            figure_data = dict(original_figure_data)

            step_images: dict[int, list[tuple[int, bytes, str, str]]] = {}
            for idx, (step_idx, frame_no, img_data, mime, role) in enumerate(all_action_images):
                if idx in selected_indices:
                    step_images.setdefault(step_idx, []).append((frame_no, img_data, mime, role))

            traj_parts: list[dict] = []
            for t in trajectory.steps:
                passed_str = "PASSED" if t.verify_passed else "FAILED"
                action_lines = render_action_transcript(t.actions)
                text = (
                    f"## Step {t.step_index + 1}: \"{t.step_name}\"\n"
                    f"Instruction sent:\n{t.instruction}\n"
                    f"Executor actions:\n{action_lines}\n"
                    f"Executor final response:\n"
                    f"{t.executor_response or '(no response)'}\n"
                    f"Verification: {t.verify_strategy} → {passed_str}\n"
                    f"Reason: {t.verify_reason}\n"
                    f"Attempts: {t.attempts}"
                    + ("\nResolved by assistant: yes" if t.resolved_by_assistant else "")
                )
                traj_parts.append({"type": "text", "text": text})

                for frame_no, img_data, mime, role in step_images.get(t.step_index, []):
                    ref = f"{role}_{frame_no}"
                    filename = f"{ref}.jpg"
                    figure_data[filename] = img_data
                    traj_parts.append({"type": "text", "text": f"[{ref}]\n"})
                    traj_parts.append({"type": "image", "data": img_data, "mime": mime})

                tool_call_count = sum(1 for a in t.actions if a.event_type == "tool_call")
                struggled = tool_call_count > 5
                include_screenshot = (
                    t.screenshot
                    and (
                        not t.verify_passed
                        or t.resolved_by_assistant
                        or t.attempts > 1
                        or struggled
                    )
                )
                if include_screenshot and t.screenshot:
                    img_bytes, img_mime = prepare_screenshot_for_llm(t.screenshot)
                    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
                    traj_ref = f"traj_{t.step_name}_{ts}.jpg"
                    figure_data[traj_ref] = img_bytes
                    caption = (
                        f"[{traj_ref}] screenshot after step {t.step_index + 1} "
                        f"\"{t.step_name}\" — {passed_str}\n"
                    )
                    traj_parts.append({"type": "text", "text": caption})
                    traj_parts.append({
                        "type": "image",
                        "data": img_bytes,
                        "mime": img_mime,
                    })

            prompt_text = prompt
            available_refs = sorted(_display_ref(ref) for ref in figure_data)
            if available_refs:
                prompt_text += (
                    "\n\n## Available figure refs\n"
                    "Only use these refs in step figures. "
                    "Do NOT invent new refs.\n"
                    + "\n".join(f"- {ref}" for ref in available_refs)
                    + "\n"
                )
            if hotspot_hint:
                prompt_text += "\n\n" + hotspot_hint

            content_parts: list[dict] = [{"type": "text", "text": prompt_text}]

            if original_figure_data:
                content_parts.append({
                    "type": "text",
                    "text": "## Original skill reference images\n",
                })
                for filename, img_bytes in original_figure_data.items():
                    content_parts.append({
                        "type": "text", "text": f"[{_display_ref(filename)}]\n",
                    })
                    content_parts.append({
                        "type": "image",
                        "data": img_bytes,
                        "mime": "image/jpeg",
                    })

            if traj_parts:
                content_parts.extend(traj_parts)
            else:
                content_parts.append({"type": "text", "text": "(no steps executed)"})

            return [{"role": "user", "content": content_parts}], figure_data, len(selected_indices)

        messages, figure_data, selected_image_count = _build_messages(img_budget)

        result_skill: Skill | None = None
        max_attempts = 3
        last_validation_error: ValidationError | None = None
        for attempt in range(max_attempts):
            try:
                output, _ = await llm.complete_structured(
                    messages,
                    response_model=Skill,
                    model=model,
                    temperature=temperature,
                )
            except ContextOverflowError as e:
                log.warning(
                    "_generate_from_trajectory: structured output failed "
                    "(attempt %d/%d): %s",
                    attempt + 1, max_attempts, e,
                )
                if attempt >= max_attempts - 1:
                    raise
                if selected_image_count > 0:
                    img_budget = max(0, selected_image_count // 2)
                else:
                    img_budget = 0
                if attempt == max_attempts - 2:
                    img_budget = 0
                messages, figure_data, selected_image_count = _build_messages(img_budget)
                log.warning(
                    "_generate_from_trajectory: retrying with image budget=%d "
                    "(%d images selected)",
                    img_budget, selected_image_count,
                )
                continue
            except ValidationError as e:
                last_validation_error = e
                log.warning(
                    "_generate_from_trajectory: structured output validation failed "
                    "(attempt %d/%d): %s",
                    attempt + 1, max_attempts, e,
                )
                continue

            # Refine mode: all original steps must be preserved
            if existing_skill is not None:
                original_names = {s.name for s in existing_skill.steps}
                refined_names = {s.name for s in output.steps}
                missing = original_names - refined_names
                if missing:
                    log.warning(
                        "_generate_from_trajectory: missing original steps %s "
                        "(attempt %d/%d); retrying",
                        missing, attempt + 1, max_attempts,
                    )
                    messages.append({"role": "assistant", "content": output.model_dump_json()})
                    messages.append({"role": "user", "content": (
                        f"Missing original steps: {', '.join(sorted(missing))}. "
                        "Keep all original steps. You may add new steps as "
                        "branch targets but never remove existing ones."
                    )})
                    continue

            if existing_skill is None and required_name:
                expected_name = to_kebab(required_name)
                generated_name = to_kebab(output.name)
                if generated_name != expected_name:
                    log.warning(
                        "_generate_from_trajectory: generated skill name %r "
                        "does not match required name %r (attempt %d/%d); retrying",
                        output.name, expected_name, attempt + 1, max_attempts,
                    )
                    messages.append({"role": "assistant", "content": output.model_dump_json()})
                    messages.append({"role": "user", "content": (
                        f"Generated skill name {output.name!r} did not match "
                        f"the required target name {expected_name!r}. Regenerate "
                        "the same intended skill with exactly that name, and keep "
                        "the description, when_to_use, inputs, steps, and success "
                        "criteria aligned with that target."
                    )})
                    continue
                output.name = expected_name

            errors = output.validate_steps()
            script_errors = output.validate_scripts()
            if not errors and not script_errors:
                result_skill = output
                break
            combined: list[str] = []
            if errors:
                log.warning(
                    "_generate_from_trajectory: step validation errors "
                    "(attempt %d/%d): %s",
                    attempt + 1, max_attempts, errors,
                )
                combined.append(
                    f"Step validation errors: {'; '.join(errors)}. "
                    "Keep original step names and ensure branch targets match."
                )
            if script_errors:
                log.warning(
                    "_generate_from_trajectory: script validation errors "
                    "(attempt %d/%d): %s",
                    attempt + 1, max_attempts, script_errors,
                )
                combined.append(
                    "Script validation errors: "
                    f"{'; '.join(script_errors)}. "
                    "Each SkillScript entry must be in exactly one of two "
                    "modes:\n"
                    "  (a) Bundle a new script: set `content` to the full "
                    "script body AND `filename` to a bare basename like "
                    "'patch.py'. The script will be written to the skill's "
                    "scripts/ directory.\n"
                    "  (b) Reference a pre-existing script in the agent's "
                    "runtime environment: leave `content` empty AND set "
                    "`filename` to an absolute path like '/usr/local/bin/foo'."
                )
            messages.append({"role": "assistant", "content": output.model_dump_json()})
            messages.append({"role": "user", "content": "\n\n".join(combined)})

        if result_skill is None:
            if existing_skill is not None:
                log.warning(
                    "_generate_from_trajectory: all attempts failed, "
                    "returning original skill",
                )
                return existing_skill
            raise RuntimeError(
                "Failed to generate skill from trajectory after all attempts"
            ) from last_validation_error

        # Preserve immutable fields from existing skill
        if existing_skill is not None:
            result_skill.name = existing_skill.name
            result_skill.source = existing_skill.source
            result_skill.metadata = existing_skill.metadata
            result_skill.figure_data = figure_data

            # Merge scripts
            if result_skill.scripts:
                original_by_name = {s.filename: s for s in existing_skill.scripts}
                refined_by_name = {s.filename: s for s in result_skill.scripts}
                original_by_name.update(refined_by_name)
                result_skill.scripts = list(original_by_name.values())
            else:
                result_skill.scripts = existing_skill.scripts
        else:
            result_skill.figure_data = figure_data
            result_skill.source = "trajectory"
            result_skill.normalize_name()

        _resolve_referenced_figures(result_skill, figure_data)

        return result_skill

    async def refine(
        self,
        skill: Skill,
        trajectory: RunTrajectory,
        llm: LLM,
        *,
        model: str | None = None,
        temperature: float = 1.0,
        evolution_guidance: str = "",
    ) -> Skill:
        """Refine a skill using its full execution trajectory.

        Sharpens step action text, tool hints, and verify_conditions
        based on what the executor actually did and what succeeded/failed.
        Preserves step order, names, count, and all skill metadata.
        """
        from protean.skills.renderer import render_skill_markdown

        prompt = REFINE_PROMPT.replace(
            "{skill_steps}", render_skill_markdown(skill),
        )
        if evolution_guidance:
            prompt += (
                "\n\n## Evolution guidance\n"
                "The evolution router selected this existing skill for a "
                "focused update. Apply this learning contract as the delta to "
                "the current skill; do not absorb unrelated trajectory details "
                "outside the stated intent and evidence.\n\n"
                f"{evolution_guidance}\n"
            )
        return await self._generate_from_trajectory(
            trajectory,
            llm,
            existing_skill=skill,
            prompt=prompt,
            model=model,
            temperature=temperature,
        )

    async def from_trajectory(
        self,
        trajectory: RunTrajectory,
        llm: LLM,
        *,
        task_context: str = "",
        target_name: str = "",
        model: str | None = None,
        temperature: float = 1.0,
        evolution_guidance: str = "",
    ) -> Skill:
        """Create a new skill from an execution trajectory.

        Distills an agent's autonomous task execution into a reusable skill.
        """
        prompt = CREATE_FROM_TRAJECTORY_PROMPT.replace(
            "{task_context}", task_context or "(no task context)",
        )
        if target_name or evolution_guidance:
            prompt += (
                "\n\n## Evolution target\n"
                "Create exactly one skill for this target. The full trajectory "
                "is available as evidence, but the skill must stay aligned with "
                "the target name and focused intent, not necessarily with every "
                "detail of the surface task.\n"
            )
            if target_name:
                prompt += f"\nRequired skill name: `{to_kebab(target_name)}`\n"
            if evolution_guidance:
                prompt += f"\n{evolution_guidance}\n"
        return await self._generate_from_trajectory(
            trajectory,
            llm,
            existing_skill=None,
            required_name=target_name,
            prompt=prompt,
            model=model,
            temperature=temperature,
        )

    # ── Recording path (batch) ───────────────────────────

    @staticmethod
    async def from_evidence(
        llm: LLM,
        evidence: EvidencePack,
        task_description: str = "",
        *,
        model: str | None = None,
        include_images: bool = True,
        max_tokens: int = 16384,
        temperature: float = 1.0,
    ) -> Skill:
        """Generate a Skill from an EvidencePack (recording analysis)."""
        import os

        # Helper: count images in the user content_parts so the next-attempt
        # budget can be derived from the actual number sent (not the configured
        # cap, which the evidence layer may have already shrunk).
        def _count_images(parts: list[dict]) -> int:
            return sum(1 for p in parts if p.get("type") == "image")

        max_attempts = 4  # 1 initial + 3 degraded retries
        attempt = 0
        budget_override: int | None = None
        original_budget_env = os.environ.get("PROTEAN_GENERATE_IMAGE_BUDGET")
        original_lean_env = os.environ.get("PROTEAN_GENERATE_TEXT_LEAN")
        try:
            while True:
                attempt += 1
                if budget_override is not None:
                    os.environ["PROTEAN_GENERATE_IMAGE_BUDGET"] = str(budget_override)
                content_parts = evidence.format_for_llm(
                    task_description, include_images,
                )
                messages: list[dict] = [
                    {
                        "role": "system",
                        "content": RECORDING_SYSTEM_PROMPT,
                    },
                    {"role": "user", "content": content_parts},
                ]
                try:
                    skill, response = await llm.complete_structured(
                        messages,
                        response_model=Skill,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    break
                except ContextOverflowError as e:
                    if attempt >= max_attempts:
                        log.error(
                            "context overflow after %d attempts; giving up", attempt,
                        )
                        raise
                    img_count = _count_images(content_parts)
                    if budget_override is None:
                        budget_override = max(0, img_count // 2)
                    else:
                        budget_override = max(0, int(budget_override * 0.6))
                    log.warning(
                        "context overflow on attempt %d (%s); retrying with "
                        "image budget=%d (was sending %d images)",
                        attempt, e, budget_override, img_count,
                    )
                    if budget_override == 0 and attempt == max_attempts - 1:
                        log.warning(
                            "engaging TEXT_LEAN fallback for final retry",
                        )
                        os.environ["PROTEAN_GENERATE_TEXT_LEAN"] = "1"
        finally:
            # Restore env so we don't leak overrides into subsequent calls.
            if original_budget_env is None:
                os.environ.pop("PROTEAN_GENERATE_IMAGE_BUDGET", None)
            else:
                os.environ["PROTEAN_GENERATE_IMAGE_BUDGET"] = original_budget_env
            if original_lean_env is None:
                os.environ.pop("PROTEAN_GENERATE_TEXT_LEAN", None)
            else:
                os.environ["PROTEAN_GENERATE_TEXT_LEAN"] = original_lean_env

        skill.normalize_name()
        skill.source = "recorded"
        skill.metadata = {
            "llm_model": response.model,
            "llm_usage": response.usage,
        }

        errors = skill.validate_steps()
        if errors:
            log.warning(
                "LLM produced invalid steps in from_evidence: %s. "
                "Retrying once.",
                errors,
            )
            # Retry once with error feedback
            messages.append({"role": "assistant", "content": skill.model_dump_json(
                exclude={"figure_data"}
            )})
            messages.append({"role": "user", "content": (
                f"The output has step validation errors: {'; '.join(errors)}. "
                "Fix the step names to be unique and ensure all branch "
                "next_step values reference existing step names."
            )})
            skill, response = await llm.complete_structured(
                messages,
                response_model=Skill,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            skill.normalize_name()
            skill.source = "recorded"
            skill.metadata = {
                "llm_model": response.model,
                "llm_usage": response.usage,
            }

        if include_images and evidence.frame_pairs:
            frame_map: dict[str, bytes] = {}
            for frame_num, pair in enumerate(evidence.frame_pairs, 1):
                frame_map[f"overview_{frame_num}"] = pair.overview_bytes
                if pair.detail_bytes:
                    frame_map[f"detail_{frame_num}"] = pair.detail_bytes
            _resolve_referenced_figures(skill, frame_map)

        return skill

    @staticmethod
    async def from_recording(
        llm: LLM,
        recording_dir: Path,
        task_description: str = "",
        *,
        model: str | None = None,
        include_images: bool = True,
        max_tokens: int = 16384,
        temperature: float = 1.0,
    ) -> Skill:
        """Build evidence from a recording directory and generate a Skill."""
        from protean.analyzer.evidence import build_evidence_pack

        evidence = build_evidence_pack(recording_dir)
        skill = await SkillBuilder.from_evidence(
            llm, evidence, task_description,
            model=model, include_images=include_images,
            max_tokens=max_tokens, temperature=temperature,
        )
        skill.metadata["recording_dir"] = str(recording_dir)
        return skill

    @staticmethod
    async def from_recording_and_save(
        llm: LLM,
        recording_dir: Path,
        output_dir: Path,
        task_description: str = "",
        *,
        model: str | None = None,
        include_images: bool = True,
        max_tokens: int = 16384,
        temperature: float = 1.0,
    ) -> tuple[Skill, Path]:
        """Generate from recording and save to disk. Returns (skill, path)."""
        from protean.skills.renderer import render_skill

        skill = await SkillBuilder.from_recording(
            llm, recording_dir, task_description,
            model=model, include_images=include_images,
            max_tokens=max_tokens, temperature=temperature,
        )
        skill_dir = output_dir / skill.name
        md_path = render_skill(skill, skill_dir)
        return skill, md_path

    def summary(self) -> str:
        """Concise summary for voice relay."""
        if not self._steps:
            return "No steps recorded yet."
        lines = [f"{len(self._steps)} steps:"]
        for i, s in enumerate(self._steps):
            tag = s.source
            lines.append(f"  {i + 1}. [{tag}] {s.action}")
        return "\n".join(lines)

"""Step verifier — verify step outcomes using AX, text, or visual strategies.

The verifier owns the fallback chain:
  1. Try the strategy declared in VerifyCondition (from skill creation).
  2. On failure, fall back through: ax_element → text_content → visual.

In full execution mode, only end-state success_criteria are checked.
Per-step verification is used in step-by-step mode.

The "visual" strategy sends a screenshot to an LLM and asks if the described
state matches. This is the most expensive check and is the last resort.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from protean.llm import LLM
    from protean.platform.base import Platform
    from protean.skills.schema import Step, VerifyCondition

log = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────

VERIFY_TIMEOUT_SEC = 360.0  # per-verification attempt timeout


class VerifyResult(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


# ── Structured output for success criteria ──────────────────

class CriterionResult(BaseModel):
    """Result for a single success criterion."""

    passed: bool = Field(description="Whether this criterion is satisfied")
    reason: str = Field(description="Brief explanation")


class CriteriaVerification(BaseModel):
    """LLM output for verifying multiple success criteria at once."""

    results: list[CriterionResult] = Field(
        description="One result per criterion, in the same order as input",
    )
    all_passed: bool = Field(
        description=(
            "Final judgment: is the overall task successful? "
            "True if the task goal is achieved, even if some individual "
            "criteria are technically unverifiable from the current screen "
            "(e.g. a VPN status panel is not visible but SSH works). "
            "False if the task clearly did not complete."
        ),
    )
    overall_reason: str = Field(
        default="",
        description="Brief explanation of the final judgment, especially "
        "when it differs from individual criteria results",
    )


@dataclass
class VerifyOutcome:
    """Result of a single verification attempt."""

    result: VerifyResult
    strategy_used: str  # which strategy actually produced this result
    reason: str = ""
    screenshot: bytes | None = None


# ── Strategy order for fallback ─────────────────────────────

_FALLBACK_ORDER: list[str] = ["ax_element", "text_content", "visual"]


# ── StepVerifier ────────────────────────────────────────────


class StepVerifier:
    """Verify step outcomes using AX queries, text checks, or LLM vision.

    Usage:
        verifier = StepVerifier(platform, llm)
        outcome = await verifier.verify_step(step)
        outcomes = await verifier.verify_success_criteria(skill)
    """

    def __init__(
        self,
        platform: Platform,
        llm: LLM,
        *,
        task: str = "",
        timeout_sec: float = VERIFY_TIMEOUT_SEC,
    ) -> None:
        self._platform = platform
        self._llm = llm
        self._task = task
        self._timeout_sec = timeout_sec

    async def verify_step(self, step: Step) -> VerifyOutcome:
        """Verify a single step using its verify_condition."""
        vc = step.verify_condition
        if vc is None:
            return VerifyOutcome(
                result=VerifyResult.PASSED,
                strategy_used="none",
                reason="No verify_condition defined; skipping verification",
            )
        return await self.verify_condition(
            vc, target_app=step.target_app, label=step.name,
        )

    async def verify_condition(
        self,
        vc: VerifyCondition,
        *,
        target_app: str = "",
        label: str = "",
    ) -> VerifyOutcome:
        """Verify a VerifyCondition with the full fallback chain.

        Used by verify_step, branch resolution, and anything else
        that needs to check a structured condition against screen state.
        """
        # Build the fallback chain: declared strategy first, then lower-priority ones
        strategies = [vc.strategy]
        found = False
        for s in _FALLBACK_ORDER:
            if s == vc.strategy:
                found = True
                continue
            if found and s not in strategies:
                strategies.append(s)

        fail_reasons: list[str] = []
        for strategy in strategies:
            try:
                outcome = await asyncio.wait_for(
                    self._try_strategy_vc(
                        strategy, vc, target_app,
                    ),
                    timeout=self._timeout_sec,
                )
                if outcome.result == VerifyResult.PASSED:
                    return outcome
                fail_reasons.append(f"{strategy}: {outcome.reason}")
                log.info(
                    "Strategy %s failed for '%s': %s. Trying next.",
                    strategy, label or "condition", outcome.reason,
                )
            except asyncio.TimeoutError:
                fail_reasons.append(f"{strategy}: timed out")
                log.warning(
                    "Strategy %s timed out for '%s'",
                    strategy, label or "condition",
                )
            except Exception as e:
                fail_reasons.append(f"{strategy}: error ({e})")
                log.warning(
                    "Strategy %s error for '%s'",
                    strategy, label or "condition", exc_info=True,
                )

        return VerifyOutcome(
            result=VerifyResult.FAILED,
            strategy_used="all_exhausted",
            reason="; ".join(fail_reasons) if fail_reasons else "All strategies failed",
        )

    async def verify_success_criteria(
        self,
        criteria: list[str],
        *,
        target_app: str = "",
    ) -> list[VerifyOutcome]:
        """Verify end-state success criteria.

        Takes ONE screenshot and checks ALL criteria in a single LLM call
        using structured output for reliable parsing.
        """
        if not criteria:
            return []

        # Take a single screenshot
        screenshot_bytes: bytes | None = None
        try:
            from protean.platform.base import (
                active_display_index,
                prepare_screenshot_for_llm,
            )

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)

            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._platform.capture_display(
                    active_display_index(self._platform), tmp_path
                ),
            )
            raw_bytes = tmp_path.read_bytes()
            tmp_path.unlink(missing_ok=True)
            screenshot_bytes, mime = prepare_screenshot_for_llm(raw_bytes)
        except Exception as e:
            return [
                VerifyOutcome(
                    result=VerifyResult.ERROR,
                    strategy_used="visual",
                    reason=f"Failed to capture screenshot: {e}",
                )
                for _ in criteria
            ]

        # Build prompt with all criteria
        app_context = f" in the application '{target_app}'" if target_app else ""
        task_context = f"\nTask context: {self._task}\n" if self._task else ""
        numbered = "\n".join(
            f"{i + 1}. {c}" for i, c in enumerate(criteria)
        )
        prompt = (
            f"Look at this screenshot{app_context}. "
            f"Check each of these success criteria:\n\n"
            f"{numbered}\n\n"
            f"{task_context}"
            f"If any criterion contains {{{{param}}}} placeholders, "
            f"use the task context above to determine the actual values.\n\n"
            f"For each criterion, determine if the current screen state "
            f"satisfies it.\n\n"
            f"Then make a FINAL JUDGMENT: is the overall task successful? "
            f"Judge by whether the task's goal was actually achieved, "
            f"not by rigid criterion matching."
        )

        try:
            messages: list[dict] = [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [(screenshot_bytes, mime)],
                },
            ]
            verification, _ = await asyncio.wait_for(
                self._llm.complete_structured(
                    messages,
                    response_model=CriteriaVerification,
                    temperature=1,
                ),
                timeout=self._timeout_sec,
            )

            outcomes: list[VerifyOutcome] = []
            for i, criterion in enumerate(criteria):
                if i < len(verification.results):
                    r = verification.results[i]
                    outcomes.append(VerifyOutcome(
                        result=VerifyResult.PASSED if r.passed else VerifyResult.FAILED,
                        strategy_used="visual",
                        reason=r.reason,
                        screenshot=screenshot_bytes,
                    ))
                else:
                    outcomes.append(VerifyOutcome(
                        result=VerifyResult.ERROR,
                        strategy_used="visual",
                        reason=f"LLM returned fewer results than criteria: {criterion}",
                        screenshot=screenshot_bytes,
                    ))

            # If LLM's final judgment overrides individual results,
            # mark all as passed/failed accordingly
            any_failed = any(o.result != VerifyResult.PASSED for o in outcomes)
            if verification.all_passed and any_failed:
                log.info(
                    "LLM final judgment: task succeeded despite %d failed criteria: %s",
                    sum(1 for o in outcomes if o.result != VerifyResult.PASSED),
                    verification.overall_reason,
                )
                for o in outcomes:
                    if o.result == VerifyResult.FAILED:
                        o.result = VerifyResult.PASSED
                        o.reason += f" [overridden: {verification.overall_reason}]"

            return outcomes

        except asyncio.TimeoutError:
            return [
                VerifyOutcome(
                    result=VerifyResult.TIMEOUT,
                    strategy_used="visual",
                    reason="Timeout verifying criteria",
                )
                for _ in criteria
            ]
        except Exception:
            log.warning("Error verifying success criteria", exc_info=True)
            return [
                VerifyOutcome(
                    result=VerifyResult.ERROR,
                    strategy_used="visual",
                    reason="Error verifying criteria",
                )
                for _ in criteria
            ]

    # ── Strategy implementations ────────────────────────────

    async def _try_strategy_vc(
        self,
        strategy: str,
        vc: VerifyCondition,
        target_app: str,
    ) -> VerifyOutcome:
        """Dispatch to the correct strategy implementation."""
        if strategy == "ax_element":
            return await self._try_ax_element(vc, target_app)
        elif strategy == "text_content":
            return await self._try_text_content(vc, target_app)
        elif strategy == "visual":
            desc = vc.description
            return await self._try_visual(desc, target_app)
        else:
            return VerifyOutcome(
                result=VerifyResult.ERROR,
                strategy_used=strategy,
                reason=f"Unknown strategy: {strategy}",
            )

    async def _try_ax_element(
        self,
        vc: VerifyCondition,
        target_app: str,
    ) -> VerifyOutcome:
        """Check the accessibility tree for a specific element.

        Uses find_elements to search by ax_title, then checks the role matches.
        """
        if not target_app:
            return VerifyOutcome(
                result=VerifyResult.FAILED,
                strategy_used="ax_element",
                reason="No target_app specified for AX verification",
            )

        search_query = vc.ax_title
        if not search_query:
            return VerifyOutcome(
                result=VerifyResult.FAILED,
                strategy_used="ax_element",
                reason="No ax_title specified for AX element search",
            )

        try:
            elements = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._platform.find_elements(target_app, search_query),
            )
        except Exception as e:
            return VerifyOutcome(
                result=VerifyResult.FAILED,
                strategy_used="ax_element",
                reason=f"find_elements failed: {e}",
            )

        if not elements:
            return VerifyOutcome(
                result=VerifyResult.FAILED,
                strategy_used="ax_element",
                reason=f"No elements found matching '{search_query}' in {target_app}",
            )

        # Check if any matching element has the expected role
        if vc.ax_role:
            matching = [e for e in elements if e.role == vc.ax_role]
            if not matching:
                found_roles = ", ".join(set(e.role for e in elements))
                return VerifyOutcome(
                    result=VerifyResult.FAILED,
                    strategy_used="ax_element",
                    reason=(
                        f"Found '{search_query}' but role mismatch: "
                        f"expected {vc.ax_role}, found [{found_roles}]"
                    ),
                )

        return VerifyOutcome(
            result=VerifyResult.PASSED,
            strategy_used="ax_element",
            reason=f"Found element '{search_query}' in {target_app}",
        )

    async def _try_text_content(
        self,
        vc: VerifyCondition,
        target_app: str,
    ) -> VerifyOutcome:
        """Check for expected text on screen using AX tree text search.

        Uses find_elements with the expected_text as query. If any element
        contains the text, verification passes.
        """
        expected = vc.expected_text
        if not expected:
            return VerifyOutcome(
                result=VerifyResult.FAILED,
                strategy_used="text_content",
                reason="No expected_text specified",
            )

        if not target_app:
            return VerifyOutcome(
                result=VerifyResult.FAILED,
                strategy_used="text_content",
                reason="No target_app specified for text content verification",
            )

        try:
            elements = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._platform.find_elements(target_app, expected),
            )
        except Exception as e:
            return VerifyOutcome(
                result=VerifyResult.FAILED,
                strategy_used="text_content",
                reason=f"find_elements failed: {e}",
            )

        if elements:
            return VerifyOutcome(
                result=VerifyResult.PASSED,
                strategy_used="text_content",
                reason=f"Found text '{expected}' on screen",
            )

        return VerifyOutcome(
            result=VerifyResult.FAILED,
            strategy_used="text_content",
            reason=f"Text '{expected}' not found on screen",
        )

    async def _try_visual(
        self,
        description: str,
        target_app: str,
    ) -> VerifyOutcome:
        """Take a screenshot and ask the LLM if the description matches.

        This is the most expensive strategy (requires an LLM call with an image).
        Used as last resort when AX and text strategies can't confirm success.
        """
        # Take a screenshot
        screenshot_bytes: bytes | None = None
        try:
            from protean.platform.base import active_display_index

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)

            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._platform.capture_display(
                    active_display_index(self._platform), tmp_path
                ),
            )
            screenshot_bytes = tmp_path.read_bytes()
            tmp_path.unlink(missing_ok=True)
        except Exception as e:
            return VerifyOutcome(
                result=VerifyResult.ERROR,
                strategy_used="visual",
                reason=f"Failed to capture screenshot: {e}",
            )

        if not screenshot_bytes:
            return VerifyOutcome(
                result=VerifyResult.ERROR,
                strategy_used="visual",
                reason="Empty screenshot",
            )

        # Resize + compress for LLM consumption
        from protean.platform.base import prepare_screenshot_for_llm

        screenshot_bytes, mime = prepare_screenshot_for_llm(screenshot_bytes)

        # Ask the LLM
        app_context = f" in the application '{target_app}'" if target_app else ""
        task_context = f"\nTask context: {self._task}\n" if self._task else ""
        prompt = (
            f"Look at this screenshot{app_context}. "
            f"Does the current screen state match this description?\n\n"
            f'"{description}"\n\n'
            f"{task_context}"
            f"If the description contains {{{{param}}}} placeholders, "
            f"use the task context above to determine the actual values.\n\n"
            f"Answer with ONLY 'YES' or 'NO' followed by a brief reason."
        )

        try:
            messages: list[dict] = [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [(screenshot_bytes, mime)],
                },
            ]
            response = await self._llm.complete(
                messages,
                temperature=1,
            )
            answer = response.content.strip().upper()
            passed = answer.startswith("YES")
            return VerifyOutcome(
                result=VerifyResult.PASSED if passed else VerifyResult.FAILED,
                strategy_used="visual",
                reason=response.content.strip(),
                screenshot=screenshot_bytes,
            )
        except Exception as e:
            return VerifyOutcome(
                result=VerifyResult.ERROR,
                strategy_used="visual",
                reason=f"LLM verification failed: {e}",
                screenshot=screenshot_bytes,
            )

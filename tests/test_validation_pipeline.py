"""Tests for the skill validation pipeline.

Covers:
  - Schema round-trip with VerifyCondition, target_app, idempotent
  - StepVerifier: AX, text_content, visual strategies + fallback chain
  - StepRunner: full mode, step-by-step mode, retry, escalation, timeouts
  - Telemetry: JSONL logging, from_report conversion
  - Parser: VerifyCondition deserialization from SKILL.md
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import patch

from protean.channels.base import AssistantChannel
from protean.skills.registry import load_skill_from_file
from protean.skills.renderer import render_skill
from protean.skills.runner import (
    ExecutionMode,
    RunMode,
    RunReport,
    StepResult,
    StepRunner,
    StepValidation,
)
from protean.skills.schema import Skill, SkillParameter, Step, VerifyCondition
from protean.skills.telemetry import (
    RunTelemetry,
    StepTelemetry,
    TelemetryLogger,
)
from protean.skills.verifier import StepVerifier, VerifyResult

# ── Fixtures / Fakes ─────────────────────────────────────


def _make_skill(**overrides: Any) -> Skill:
    """Create a Skill with sensible defaults for testing."""
    defaults = dict(
        name="test-skill",
        description="A test skill for validation.",
        goal="Complete the test workflow.",
        steps=[
            Step(
                name="open-settings",
                action="Click the Settings button in TestApp.",
                target_app="TestApp",
                tool="find_elements(app='TestApp', query='Settings') -> click_at(center_x, center_y)",
                verify_condition=VerifyCondition(
                    strategy="ax_element",
                    ax_role="AXButton",
                    ax_title="Settings",
                ),
            ),
            Step(
                name="enable-dark-mode",
                action="Toggle the Dark Mode switch.",
                target_app="TestApp",
                tool="click_at(x, y)",
                verify_condition=VerifyCondition(
                    strategy="text_content",
                    expected_text="Dark Mode: On",
                ),
            ),
            Step(
                name="save-and-close",
                action="Click Save and close the settings.",
                target_app="TestApp",
                tool="",
                verify_condition=VerifyCondition(
                    strategy="visual",
                    description="Settings panel is closed, dark theme is applied",
                ),
                idempotent=False,
            ),
        ],
        success_criteria=["Dark mode is enabled", "Settings saved"],
        when_to_use=["enable dark mode"],
        when_not_to_use=["light mode only apps"],
        parameters=[SkillParameter(name="theme", description="Theme to apply")],
        tags=["settings", "theme"],
    )
    defaults.update(overrides)
    return Skill(**defaults)


@dataclass(frozen=True)
class FakeElementInfo:
    """Matches the Platform.ElementInfo interface."""
    role: str
    label: str
    center_x: int = 100
    center_y: int = 100
    width: int = 50
    height: int = 50


class FakePlatform:
    """Fake Platform for testing (sync methods called via run_in_executor)."""

    def __init__(
        self,
        elements: list[FakeElementInfo] | None = None,
        screenshot_bytes: bytes = b"fake-png",
    ) -> None:
        self._elements = elements or []
        self._screenshot_bytes = screenshot_bytes
        self.notify_calls: list[tuple[str, str]] = []
        self.activated_apps: list[str] = []

    def find_elements(self, app: str, query: str) -> list[FakeElementInfo]:
        return [e for e in self._elements if query.lower() in e.label.lower()]

    def capture_display(self, display_index: int, output_path: Path) -> None:
        output_path.write_bytes(self._screenshot_bytes)

    def get_displays(self) -> list:
        from protean.platform.base import DisplayInfo
        return [DisplayInfo(display_id=1, display_index=1, width=1920, height=1080, is_primary=True)]

    def get_active_window(self):
        return None

    def get_cursor_position(self) -> tuple[int, int]:
        return (960, 540)

    def notify(self, title: str, message: str, *, sound: bool = True) -> None:
        self.notify_calls.append((title, message))

    def activate_app(self, app_name: str) -> None:
        self.activated_apps.append(app_name)


class FakeLLM:
    """Fake LLM that returns canned responses."""

    def __init__(self, response_text: str = "YES. The screen matches.") -> None:
        self._response_text = response_text

    async def complete(self, messages: list[dict], **kwargs: Any) -> Any:
        @dataclass
        class FakeResponse:
            content: str
            model: str = "fake"
            usage: dict = field(default_factory=dict)

        return FakeResponse(content=self._response_text)


class FakeExecutorEvent:
    """Matches ExecutorEvent interface."""
    def __init__(self, type_str: str, message: str = "", error: str = "", tool_name: str = ""):
        from protean.executor import ExecutorEventType
        self.type = ExecutorEventType(type_str)
        self.message = message
        self.error = error
        self.tool_name = tool_name
        self.tool_args: dict = {}
        self.result = ""


class FakeExecutor:
    """Fake ExecutorProvider for testing."""

    def __init__(self, done_message: str = "Skill executed successfully.") -> None:
        self._done_message = done_message
        self._started = False
        self._messages: list[str] = []
        self._start_count = 0
        self._send_count = 0

    async def start_task(self, instruction: str, context: str = "", images: Any = None, **kwargs: Any) -> None:
        self._started = True
        self._start_count += 1

    async def send_message(self, message: str) -> None:
        self._messages.append(message)
        self._send_count += 1

    async def get_events(self) -> AsyncIterator:
        yield FakeExecutorEvent("done", message=self._done_message)

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeHumanChannel:
    """Fake AssistantChannel for testing escalation."""

    def __init__(self, answer: str = "done") -> None:
        self._answer = answer
        self.asked: list[str] = []

    async def ask(self, question: str, screenshot: bytes | None = None) -> str:
        self.asked.append(question)
        return self._answer

    async def confirm(self, message: str) -> bool:
        self.asked.append(message)
        return self._answer.lower() in ("yes", "y", "done")


# ═══════════════════════════════════════════════════════════
# Schema round-trip tests
# ═══════════════════════════════════════════════════════════


class TestSchemaRoundTrip:
    """Test Skill → SKILL.md → Skill round-trip with new fields."""

    def test_verify_condition_roundtrip(self):
        skill = _make_skill()

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / skill.name
            md_path = render_skill(skill, out)
            content = md_path.read_text()

            # Check rendering
            assert "**App:** TestApp" in content
            assert "**Verify Condition:** strategy=ax_element" in content
            assert "ax_role=AXButton" in content
            assert "ax_title=Settings" in content
            assert "expected_text=Dark Mode: On" in content
            assert "strategy=visual" in content
            assert "**Idempotent:** no" in content

            # Parse back
            parsed = load_skill_from_file(md_path)
            assert len(parsed.steps) == 3

            # Step 0: ax_element
            s0 = parsed.steps[0]
            assert s0.target_app == "TestApp"
            assert s0.verify_condition is not None
            assert s0.verify_condition.strategy == "ax_element"
            assert s0.verify_condition.ax_role == "AXButton"
            assert s0.verify_condition.ax_title == "Settings"
            assert s0.idempotent is True

            # Step 1: text_content
            s1 = parsed.steps[1]
            assert s1.verify_condition is not None
            assert s1.verify_condition.strategy == "text_content"
            assert s1.verify_condition.expected_text == "Dark Mode: On"

            # Step 2: visual + non-idempotent
            s2 = parsed.steps[2]
            assert s2.verify_condition is not None
            assert s2.verify_condition.strategy == "visual"
            assert "dark theme" in s2.verify_condition.description
            assert s2.idempotent is False

    def test_no_verify_condition_roundtrip(self):
        """Steps without verify_condition should round-trip cleanly."""
        skill = Skill(
            name="simple-skill",
            description="No verification.",
            steps=[
                Step(name="do-something", action="Just do it.", verify="Check it."),
            ],
        )

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "simple-skill"
            md_path = render_skill(skill, out)
            parsed = load_skill_from_file(md_path)

            assert len(parsed.steps) == 1
            assert parsed.steps[0].verify_condition is None
            assert parsed.steps[0].target_app == ""

    def test_success_criteria_preserved(self):
        skill = _make_skill()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / skill.name
            md_path = render_skill(skill, out)
            parsed = load_skill_from_file(md_path)
            assert parsed.success_criteria == ["Dark mode is enabled", "Settings saved"]


# ═══════════════════════════════════════════════════════════
# StepVerifier tests
# ═══════════════════════════════════════════════════════════


class TestStepVerifier:
    """Test the 3 verification strategies + fallback chain."""

    async def test_ax_element_passes(self):
        """AX element strategy passes when element is found with matching role."""
        platform = FakePlatform(elements=[
            FakeElementInfo(role="AXButton", label="Settings"),
        ])
        verifier = StepVerifier(platform, FakeLLM())  # type: ignore

        step = Step(
            name="open-settings",
            action="Click Settings.",
            target_app="TestApp",
            verify_condition=VerifyCondition(
                strategy="ax_element",
                ax_role="AXButton",
                ax_title="Settings",
            ),
        )
        outcome = await verifier.verify_step(step)
        assert outcome.result == VerifyResult.PASSED
        assert outcome.strategy_used == "ax_element"

    async def test_ax_element_role_mismatch(self):
        """AX strategy fails when role doesn't match."""
        platform = FakePlatform(elements=[
            FakeElementInfo(role="AXStaticText", label="Settings"),
        ])
        verifier = StepVerifier(platform, FakeLLM())  # type: ignore

        step = Step(
            name="open-settings",
            action="Click Settings.",
            target_app="TestApp",
            verify_condition=VerifyCondition(
                strategy="ax_element",
                ax_role="AXButton",
                ax_title="Settings",
            ),
        )
        outcome = await verifier.verify_step(step)
        # AX element fails because of role mismatch, but fallback to text/visual
        # Since FakePlatform has "Settings" element, text_content should pick it up
        # depending on whether expected_text is set. It's not, so it falls through
        # to visual, which returns YES.
        assert outcome.result == VerifyResult.PASSED
        assert outcome.strategy_used == "visual"

    async def test_text_content_passes(self):
        """Text content strategy passes when expected text is found."""
        platform = FakePlatform(elements=[
            FakeElementInfo(role="AXStaticText", label="Dark Mode: On"),
        ])
        verifier = StepVerifier(platform, FakeLLM())  # type: ignore

        step = Step(
            name="enable-dark-mode",
            action="Toggle switch.",
            target_app="TestApp",
            verify_condition=VerifyCondition(
                strategy="text_content",
                expected_text="Dark Mode: On",
            ),
        )
        outcome = await verifier.verify_step(step)
        assert outcome.result == VerifyResult.PASSED
        assert outcome.strategy_used == "text_content"

    async def test_text_content_not_found(self):
        """Text content fails when text is not on screen."""
        platform = FakePlatform(elements=[])
        verifier = StepVerifier(platform, FakeLLM(response_text="NO. The text is not visible."))  # type: ignore

        step = Step(
            name="enable-dark-mode",
            action="Toggle switch.",
            target_app="TestApp",
            verify_condition=VerifyCondition(
                strategy="text_content",
                expected_text="Dark Mode: On",
            ),
        )
        outcome = await verifier.verify_step(step)
        assert outcome.result == VerifyResult.FAILED

    async def test_visual_passes(self):
        """Visual strategy passes when LLM says YES."""
        platform = FakePlatform()
        verifier = StepVerifier(platform, FakeLLM(response_text="YES. Dark theme applied."))  # type: ignore

        step = Step(
            name="apply-theme",
            action="Apply dark theme.",
            target_app="TestApp",
            verify_condition=VerifyCondition(
                strategy="visual",
                description="Dark theme is applied to the entire window",
            ),
        )
        outcome = await verifier.verify_step(step)
        assert outcome.result == VerifyResult.PASSED
        assert outcome.strategy_used == "visual"

    async def test_visual_fails(self):
        """Visual strategy fails when LLM says NO."""
        platform = FakePlatform()
        verifier = StepVerifier(platform, FakeLLM(response_text="NO. Still light theme."))  # type: ignore

        step = Step(
            name="apply-theme",
            action="Apply dark theme.",
            target_app="TestApp",
            verify_condition=VerifyCondition(
                strategy="visual",
                description="Dark theme is applied",
            ),
        )
        outcome = await verifier.verify_step(step)
        assert outcome.result == VerifyResult.FAILED
        assert outcome.strategy_used == "all_exhausted"

    async def test_no_verify_condition_skips(self):
        """Steps without verify_condition skip verification."""
        platform = FakePlatform()
        verifier = StepVerifier(platform, FakeLLM())  # type: ignore

        step = Step(name="do-something", action="Just do it.")
        outcome = await verifier.verify_step(step)
        assert outcome.result == VerifyResult.PASSED
        assert outcome.strategy_used == "none"

    async def test_fallback_chain(self):
        """When primary strategy fails, try fallback chain."""
        # No elements in platform, so ax_element fails.
        # text_content fails too (no expected_text on ax_element VC).
        # visual succeeds via LLM.
        platform = FakePlatform(elements=[])
        verifier = StepVerifier(platform, FakeLLM(response_text="YES. Matches."))  # type: ignore

        step = Step(
            name="open-settings",
            action="Click Settings.",
            target_app="TestApp",
            verify_condition=VerifyCondition(
                strategy="ax_element",
                ax_role="AXButton",
                ax_title="Settings",
            ),
        )
        outcome = await verifier.verify_step(step)
        assert outcome.result == VerifyResult.PASSED
        assert outcome.strategy_used == "visual"

    async def test_no_target_app_ax_fails(self):
        """AX strategy fails without target_app, falls to visual."""
        platform = FakePlatform()
        verifier = StepVerifier(platform, FakeLLM(response_text="YES."))  # type: ignore

        step = Step(
            name="do-thing",
            action="Do it.",
            target_app="",
            verify_condition=VerifyCondition(
                strategy="ax_element",
                ax_role="AXButton",
                ax_title="OK",
            ),
        )
        outcome = await verifier.verify_step(step)
        assert outcome.result == VerifyResult.PASSED
        assert outcome.strategy_used == "visual"

    async def test_verify_success_criteria(self):
        """End-state verification checks each criterion."""
        platform = FakePlatform()
        verifier = StepVerifier(
            platform,
            FakeLLM(response_text="YES. Matches."),  # type: ignore
        )

        outcomes = await verifier.verify_success_criteria(
            ["Dark mode enabled", "Settings saved"],
            target_app="TestApp",
        )
        assert len(outcomes) == 2
        assert all(o.result == VerifyResult.PASSED for o in outcomes)

    async def test_verify_success_criteria_partial_failure(self):
        """Some criteria pass, some fail."""
        # We need the LLM to return different responses per call.
        # Simple approach: use a counter in the fake LLM.
        call_count = 0

        class AlternatingLLM:
            async def complete(self, messages, **kwargs):
                nonlocal call_count
                call_count += 1
                text = "YES." if call_count == 1 else "NO."
                @dataclass
                class R:
                    content: str = text
                    model: str = "fake"
                    usage: dict = field(default_factory=dict)
                return R()

        platform = FakePlatform()
        verifier = StepVerifier(platform, AlternatingLLM())  # type: ignore

        outcomes = await verifier.verify_success_criteria(
            ["Should pass", "Should fail"],
        )
        assert outcomes[0].result == VerifyResult.PASSED
        assert outcomes[1].result == VerifyResult.FAILED


# ═══════════════════════════════════════════════════════════
# StepRunner tests
# ═══════════════════════════════════════════════════════════


class TestStepRunnerFull:
    """Test StepRunner in full execution mode."""

    async def test_run_validate_passes(self):
        """Full mode + validate: execute, then check success_criteria."""
        executor = FakeExecutor(done_message="All done.")
        platform = FakePlatform()
        llm = FakeLLM(response_text="YES. Criteria met.")

        runner = StepRunner(
            executor, platform, llm,  # type: ignore
            execution_mode=ExecutionMode.FULL,
        )
        skill = _make_skill()
        report = await runner.run(skill, mode=RunMode.VALIDATE)

        assert report.passed
        assert report.execution_result == "All done."
        assert len(report.steps) == 2  # 2 success_criteria
        assert report.duration > 0

    async def test_run_validate_no_criteria(self):
        """Full mode with no success_criteria passes by default."""
        executor = FakeExecutor()
        platform = FakePlatform()
        llm = FakeLLM()

        runner = StepRunner(executor, platform, llm, execution_mode=ExecutionMode.FULL)  # type: ignore
        skill = _make_skill(success_criteria=[])
        report = await runner.run(skill, mode=RunMode.VALIDATE)

        assert report.passed
        assert len(report.steps) == 1
        assert report.steps[0].strategy_used == "none"

    async def test_run_validate_fails(self):
        """Full mode fails when success_criteria don't pass."""
        executor = FakeExecutor()
        platform = FakePlatform()
        llm = FakeLLM(response_text="NO. Criteria not met.")

        runner = StepRunner(executor, platform, llm, execution_mode=ExecutionMode.FULL)  # type: ignore
        skill = _make_skill()
        report = await runner.run(skill, mode=RunMode.VALIDATE)

        assert not report.passed
        assert report.aborted_at == 0

    async def test_run_assisted_escalates(self):
        """Assisted mode escalates to human when criteria fail."""
        executor = FakeExecutor()
        platform = FakePlatform()
        llm = FakeLLM(response_text="NO. Not matching.")
        human = FakeHumanChannel(answer="yes")

        runner = StepRunner(
            executor, platform, llm,  # type: ignore
            human=human,
            execution_mode=ExecutionMode.FULL,
        )
        skill = _make_skill()
        report = await runner.run(skill, mode=RunMode.ASSISTED)

        # Human overrides the failure
        assert report.passed
        assert len(human.asked) > 0

    async def test_notification_sent(self):
        """Completion notification is sent after run."""
        executor = FakeExecutor()
        platform = FakePlatform()
        llm = FakeLLM(response_text="YES.")

        runner = StepRunner(executor, platform, llm, execution_mode=ExecutionMode.FULL)  # type: ignore
        skill = _make_skill()
        await runner.run(skill)

        assert len(platform.notify_calls) == 1
        assert "passed" in platform.notify_calls[0][0]


class TestStepRunnerStepByStep:
    """Test StepRunner in step-by-step mode."""

    async def test_all_steps_pass(self):
        """All steps pass verification."""
        platform = FakePlatform(elements=[
            FakeElementInfo(role="AXButton", label="Settings"),
            FakeElementInfo(role="AXStaticText", label="Dark Mode: On"),
        ])
        executor = FakeExecutor()
        llm = FakeLLM(response_text="YES.")

        runner = StepRunner(
            executor, platform, llm,  # type: ignore
            execution_mode=ExecutionMode.STEP_BY_STEP,
        )
        skill = _make_skill()
        report = await runner.run(skill, mode=RunMode.VALIDATE)

        assert report.passed
        assert len(report.steps) == 3

    async def test_step_fails_and_aborts(self):
        """When 2 consecutive steps fail, the run aborts."""
        platform = FakePlatform(elements=[])  # No elements, AX fails
        executor = FakeExecutor()
        llm = FakeLLM(response_text="NO. Not found.")

        runner = StepRunner(
            executor, platform, llm,  # type: ignore
            execution_mode=ExecutionMode.STEP_BY_STEP,
        )
        skill = _make_skill()
        report = await runner.run(skill, mode=RunMode.VALIDATE)

        assert not report.passed
        # First failure continues, second triggers consecutive_failures >= 2 abort
        assert report.aborted_at == 0  # first failed step
        assert len(report.steps) == 2  # 2 steps attempted before abort
        # First step should have tried STEP_RETRY_BUDGET times
        assert report.steps[0].attempts == 3

    async def test_non_idempotent_no_retry(self):
        """Non-idempotent steps are not retried."""
        platform = FakePlatform(elements=[])
        executor = FakeExecutor()
        llm = FakeLLM(response_text="NO.")

        # Make step 0 non-idempotent
        skill = _make_skill(steps=[
            Step(
                name="send-email",
                action="Click send.",
                target_app="Mail",
                verify_condition=VerifyCondition(
                    strategy="ax_element",
                    ax_role="AXButton",
                    ax_title="Sent",
                ),
                idempotent=False,
            ),
        ])

        runner = StepRunner(
            executor, platform, llm,  # type: ignore
            execution_mode=ExecutionMode.STEP_BY_STEP,
        )
        report = await runner.run(skill, mode=RunMode.VALIDATE)

        assert not report.passed
        assert report.steps[0].attempts == 1  # No retry

    async def test_assisted_mode_human_resolves(self):
        """In assisted mode, human can resolve a failed step."""
        platform = FakePlatform(elements=[])
        executor = FakeExecutor()
        llm = FakeLLM(response_text="NO.")
        human = FakeHumanChannel(answer="done")

        skill = _make_skill(steps=[
            Step(
                name="do-something",
                action="Click it.",
                target_app="TestApp",
                verify_condition=VerifyCondition(
                    strategy="ax_element",
                    ax_role="AXButton",
                    ax_title="OK",
                ),
            ),
        ])

        runner = StepRunner(
            executor, platform, llm,  # type: ignore
            human=human,
            execution_mode=ExecutionMode.STEP_BY_STEP,
        )
        report = await runner.run(skill, mode=RunMode.ASSISTED)

        assert report.passed
        assert report.steps[0].strategy_used == "human"


# ═══════════════════════════════════════════════════════════
# Telemetry tests
# ═══════════════════════════════════════════════════════════


class TestTelemetry:
    """Test the telemetry JSONL logging system."""

    def test_run_telemetry_to_jsonl(self):
        entry = RunTelemetry(
            skill_name="test-skill",
            timestamp="2026-04-05T12:00:00",
            mode="validate",
            execution_mode="full",
            steps=[
                StepTelemetry(step_index=0, verify_result="passed", strategy_used="ax_element"),
                StepTelemetry(step_index=1, verify_result="failed", strategy_used="visual"),
            ],
            total_duration_ms=5000,
            outcome="failed",
            failure_step=1,
            failure_reason="Visual check failed",
        )
        line = entry.to_jsonl()
        data = json.loads(line)

        assert data["skill_name"] == "test-skill"
        assert data["mode"] == "validate"
        assert len(data["steps"]) == 2
        assert data["steps"][0]["verify_result"] == "passed"
        assert data["failure_step"] == 1

    def test_telemetry_logger_write_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            logger = TelemetryLogger(Path(d) / "telemetry")

            entry = RunTelemetry(
                skill_name="test-skill",
                timestamp="2026-04-05T12:00:00",
                mode="validate",
                execution_mode="full",
                outcome="passed",
            )
            logger.log(entry)

            # Write another
            entry2 = RunTelemetry(
                skill_name="other-skill",
                timestamp="2026-04-05T12:01:00",
                mode="assisted",
                execution_mode="step_by_step",
                outcome="failed",
            )
            logger.log(entry2)

            # Read all
            runs = logger.read_runs()
            assert len(runs) == 2

            # Read filtered
            filtered = logger.read_runs(skill_name="test-skill")
            assert len(filtered) == 1
            assert filtered[0].outcome == "passed"

    def test_from_report(self):
        with tempfile.TemporaryDirectory() as d:
            logger = TelemetryLogger(Path(d) / "telemetry")

            report = RunReport(
                skill_name="test-skill",
                mode=RunMode.VALIDATE,
                steps=[
                    StepValidation(index=0, result=StepResult.PASSED, attempts=1),
                    StepValidation(index=1, result=StepResult.FAILED, attempts=3, reason="AX failed"),
                ],
                duration=2.5,
            )

            entry = logger.from_report(report, execution_mode="full")
            assert entry.skill_name == "test-skill"
            assert entry.outcome == "failed"
            assert entry.total_duration_ms == 2500
            assert len(entry.steps) == 2
            assert entry.failure_step == 1

    async def test_runner_logs_telemetry(self):
        """StepRunner logs telemetry when TelemetryLogger is provided."""
        with tempfile.TemporaryDirectory() as d:
            tel_logger = TelemetryLogger(Path(d) / "telemetry")
            executor = FakeExecutor()
            platform = FakePlatform()
            llm = FakeLLM(response_text="YES.")

            runner = StepRunner(
                executor, platform, llm,  # type: ignore
                telemetry=tel_logger,
                execution_mode=ExecutionMode.FULL,
            )
            skill = _make_skill()
            await runner.run(skill)

            runs = tel_logger.read_runs()
            assert len(runs) == 1
            assert runs[0].skill_name == "test-skill"


# ═══════════════════════════════════════════════════════════
# Parser edge cases
# ═══════════════════════════════════════════════════════════


class TestParserEdgeCases:
    """Test SKILL.md parser with various formats."""

    def test_parse_verify_condition_all_strategies(self):
        """Parse VerifyCondition for each strategy type."""
        for strategy, extra_field, extra_value in [
            ("ax_element", "ax_role", "AXSheet"),
            ("text_content", "expected_text", "Success!"),
            ("visual", "description", "Green checkmark visible"),
        ]:
            skill = Skill(
                name=f"test-{strategy}",
                description="Test.",
                steps=[
                    Step(
                        name="test-step",
                        action="Do it.",
                        target_app="App",
                        verify_condition=VerifyCondition(
                            strategy=strategy,  # type: ignore
                            **{extra_field: extra_value},
                        ),
                    ),
                ],
            )

            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / skill.name
                md_path = render_skill(skill, out)
                parsed = load_skill_from_file(md_path)

                vc = parsed.steps[0].verify_condition
                assert vc is not None
                assert vc.strategy == strategy
                assert getattr(vc, extra_field) == extra_value

    def test_parse_step_without_verify_condition(self):
        """Steps without verify_condition should parse to None."""
        skill = Skill(
            name="no-vc",
            description="No VC.",
            steps=[Step(name="test", action="Do it.")],
        )
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "no-vc"
            md_path = render_skill(skill, out)
            parsed = load_skill_from_file(md_path)
            assert parsed.steps[0].verify_condition is None

    def test_parse_idempotent_flag(self):
        """Idempotent flag round-trips correctly."""
        skill = Skill(
            name="idem-test",
            description="Idempotent test.",
            steps=[
                Step(name="safe-step", action="Retry OK.", idempotent=True),
                Step(name="unsafe-step", action="Send email.", idempotent=False),
            ],
        )
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "idem-test"
            md_path = render_skill(skill, out)
            parsed = load_skill_from_file(md_path)
            assert parsed.steps[0].idempotent is True
            assert parsed.steps[1].idempotent is False

    def test_registry_load_logs_on_error(self):
        """Registry logs warnings when SKILL.md fails to parse."""
        from protean.skills.registry import SkillRegistry

        with tempfile.TemporaryDirectory() as d:
            bad_dir = Path(d) / "broken-skill"
            bad_dir.mkdir()
            bad_md = bad_dir / "SKILL.md"
            # Write invalid YAML frontmatter
            bad_md.write_text("---\n: :\n---\nBroken")

            registry = SkillRegistry(Path(d))
            with patch("protean.skills.registry.log") as mock_log:
                count = registry.load_all()
                assert count == 0
                # Should have logged a warning
                mock_log.warning.assert_called()


# ═══════════════════════════════════════════════════════════
# AssistantChannel protocol tests
# ═══════════════════════════════════════════════════════════


class TestAssistantChannelProtocol:
    """Test that our channel implementations satisfy the protocol."""

    def test_fake_satisfies_protocol(self):
        ch = FakeHumanChannel()
        assert isinstance(ch, AssistantChannel)

    def test_cli_satisfies_protocol(self):
        from protean.channels.cli import CLIAssistantChannel
        assert hasattr(CLIAssistantChannel, "ask")
        assert hasattr(CLIAssistantChannel, "confirm")


# ═══════════════════════════════════════════════════════════
# RunReport tests
# ═══════════════════════════════════════════════════════════


class TestRunReport:
    """Test RunReport properties."""

    def test_passed_all_pass(self):
        report = RunReport(
            skill_name="test",
            mode=RunMode.VALIDATE,
            steps=[
                StepValidation(index=0, result=StepResult.PASSED),
                StepValidation(index=1, result=StepResult.PASSED),
            ],
        )
        assert report.passed is True
        assert report.aborted_at is None

    def test_not_passed_on_failure(self):
        report = RunReport(
            skill_name="test",
            mode=RunMode.VALIDATE,
            steps=[
                StepValidation(index=0, result=StepResult.PASSED),
                StepValidation(index=1, result=StepResult.FAILED),
            ],
        )
        assert report.passed is False
        assert report.aborted_at == 1

    def test_aborted_at_needs_human(self):
        report = RunReport(
            skill_name="test",
            mode=RunMode.ASSISTED,
            steps=[
                StepValidation(index=0, result=StepResult.NEEDS_HUMAN),
            ],
        )
        assert report.aborted_at == 0

    def test_empty_steps_passes(self):
        report = RunReport(skill_name="test", mode=RunMode.VALIDATE)
        assert report.passed is True


# ═══════════════════════════════════════════════════════════
# Gap 1: Retry strategy escalation tests
# ═══════════════════════════════════════════════════════════


class TestRetryStrategies:
    """Test the 3-tier retry strategy: simple → wait+restart → LLM-guided fix."""

    async def test_retry_strategy_names(self):
        """Strategy names map to attempt numbers."""
        assert StepRunner._retry_strategy_name(0) == "simple"
        assert StepRunner._retry_strategy_name(1) == "wait+restart"
        assert StepRunner._retry_strategy_name(2) == "llm_guided_fix"

    async def test_wait_and_restart_activates_app(self):
        """Attempt 2 waits and re-activates the target app."""
        platform = FakePlatform()
        executor = FakeExecutor()
        llm = FakeLLM(response_text="NO.")

        runner = StepRunner(executor, platform, llm, execution_mode=ExecutionMode.STEP_BY_STEP)  # type: ignore

        step = Step(
            name="test", action="Do it.", target_app="MyApp",
            verify_condition=VerifyCondition(strategy="ax_element", ax_title="X"),
        )
        await runner._retry_wait_and_restart(step)
        # Platform.activate_app should have been called
        assert "MyApp" in platform.activated_apps

    async def test_wait_and_restart_no_target_app(self):
        """Attempt 2 gracefully handles missing target_app."""
        platform = FakePlatform()
        executor = FakeExecutor()
        llm = FakeLLM(response_text="NO.")

        runner = StepRunner(executor, platform, llm, execution_mode=ExecutionMode.STEP_BY_STEP)  # type: ignore

        step = Step(name="test", action="Do it.")
        # Should not raise
        await runner._retry_wait_and_restart(step)

    async def test_llm_guided_fix_sends_correction(self):
        """Attempt 3 gets LLM corrective action and sends to executor."""
        platform = FakePlatform(screenshot_bytes=b"fake_screenshot")
        executor = FakeExecutor()
        llm = FakeLLM(response_text="Click OK to dismiss the dialog.")

        runner = StepRunner(executor, platform, llm, execution_mode=ExecutionMode.STEP_BY_STEP)  # type: ignore

        skill = _make_skill()
        step = skill.steps[0]
        result = await runner._retry_llm_guided_fix(
            skill, step, 0, "Element not found", b"screenshot_data",
        )
        assert result == "Click OK to dismiss the dialog."
        # Corrective action should have been sent to executor via send_message
        assert len(executor._messages) == 1
        assert "Correction for step 1" in executor._messages[0]

    async def test_retry_escalation_in_step_by_step(self):
        """Step-by-step mode escalates through retry strategies."""
        call_log: list[str] = []

        class TrackingPlatform(FakePlatform):
            def activate_app(self, app_name: str) -> None:
                call_log.append(f"activate:{app_name}")
                self.activated_apps.append(app_name)

        platform = TrackingPlatform(elements=[])
        executor = FakeExecutor()
        llm = FakeLLM(response_text="NO. Not found.")

        runner = StepRunner(
            executor, platform, llm,  # type: ignore
            execution_mode=ExecutionMode.STEP_BY_STEP,
        )

        # Single step that will fail all 3 attempts
        skill = _make_skill(steps=[
            Step(
                name="test",
                action="Do it.",
                target_app="TestApp",
                verify_condition=VerifyCondition(strategy="visual", description="Done"),
                idempotent=True,
            ),
        ])
        report = await runner.run(skill, mode=RunMode.VALIDATE)

        assert not report.passed
        assert report.steps[0].attempts == 3
        # Should have activated app during wait+restart (attempt 2)
        assert "activate:TestApp" in call_log


# ═══════════════════════════════════════════════════════════
# Gap 2: Consecutive & total failure threshold tests
# ═══════════════════════════════════════════════════════════


class TestFailureThresholds:
    """Test consecutive failure abort and total failure escalation."""

    async def test_single_failure_continues(self):
        """A single step failure does NOT abort the skill — next step is attempted."""
        # Use a tracking LLM that fails step 0 but passes step 1+
        step_llm_pass = [False]  # toggle per step

        class PerStepLLM:
            async def complete(self, messages, **kwargs):
                text = "YES." if step_llm_pass[0] else "NO."
                @dataclass
                class R:
                    content: str = text
                    model: str = "fake"
                    usage: dict = field(default_factory=dict)
                return R()

        platform = FakePlatform(elements=[])  # no AX elements → always falls to visual
        executor = FakeExecutor()
        llm = PerStepLLM()

        runner = StepRunner(
            executor, platform, llm,  # type: ignore
            execution_mode=ExecutionMode.STEP_BY_STEP,
        )

        # Hook _execute_single_step to toggle LLM for step 1+
        original_execute = runner._execute_single_step

        async def tracking_execute(skill, step, step_index, skill_dir):
            step_llm_pass[0] = step_index >= 1  # step 0 fails, 1+ pass
            return await original_execute(skill, step, step_index, skill_dir)

        runner._execute_single_step = tracking_execute

        skill = _make_skill()
        report = await runner.run(skill, mode=RunMode.VALIDATE)

        # First step failed, but second should have been attempted
        assert len(report.steps) >= 2
        assert report.steps[0].result == StepResult.FAILED

    async def test_two_consecutive_failures_abort(self):
        """Two consecutive step failures abort the skill."""
        platform = FakePlatform(elements=[])
        executor = FakeExecutor()
        llm = FakeLLM(response_text="NO.")

        runner = StepRunner(
            executor, platform, llm,  # type: ignore
            execution_mode=ExecutionMode.STEP_BY_STEP,
        )
        skill = _make_skill()  # 3 steps, all will fail
        report = await runner.run(skill, mode=RunMode.VALIDATE)

        assert not report.passed
        # Should have attempted exactly 2 steps then aborted
        assert len(report.steps) == 2

    async def test_total_failures_escalate_assisted(self):
        """In assisted mode, 3 total (non-consecutive) failures escalates entire skill.

        Creates a pattern where steps alternate fail→pass→fail→pass→fail,
        so consecutive_failures never reaches 2, but total_failures reaches 3.
        """
        step_calls = [0]

        class AlternatingPlatform(FakePlatform):
            """Alternates: odd steps fail (empty elements), even steps pass."""
            def find_elements(self, app: str, query: str) -> list:
                return []  # always fail AX — visual will decide

        class AlternatingLLM:
            """Steps 0, 2, 4 fail visual; steps 1, 3 pass visual."""
            def __init__(self):
                self._call_count = 0
                # Track which step we're verifying by monitoring call patterns.
                # Each step in step_by_step mode triggers multiple LLM calls
                # (one per strategy in fallback chain). We use a flag approach.
                self._current_step_passes = False

            async def complete(self, messages, **kwargs):
                self._call_count += 1
                text = "YES." if self._current_step_passes else "NO."
                @dataclass
                class R:
                    content: str = text
                    model: str = "fake"
                    usage: dict = field(default_factory=dict)
                return R()

        alt_llm = AlternatingLLM()

        # Track which step the runner is on by patching _execute_single_step
        human_calls: list[str] = []
        escalate_calls: list[str] = []

        class TrackingHuman(FakeHumanChannel):
            async def ask(self, question: str, screenshot: bytes | None = None) -> str:
                human_calls.append(question)
                if "failed verification" in question:
                    # This is _escalate_to_human for the full skill
                    escalate_calls.append(question)
                    return "skip"
                return "skip"  # don't resolve individual steps

        # Create a custom runner that tracks step execution
        platform = AlternatingPlatform()
        executor = FakeExecutor()
        human = TrackingHuman()

        runner = StepRunner(
            executor, platform, alt_llm,  # type: ignore
            execution_mode=ExecutionMode.STEP_BY_STEP,
            human=human,
        )

        # 5 steps: step 0 fails, 1 passes, 2 fails, 3 passes, 4 fails
        # By setting the LLM behavior per-step from the runner's perspective
        # we need to hook into the step execution.
        original_execute = runner._execute_single_step

        async def tracking_execute(skill, step, step_index, skill_dir):
            # Toggle LLM behavior based on step index: even=fail, odd=pass
            alt_llm._current_step_passes = (step_index % 2 == 1)
            return await original_execute(skill, step, step_index, skill_dir)

        runner._execute_single_step = tracking_execute

        skill = _make_skill(steps=[
            Step(
                name=f"step-{i}", action="Do it.", target_app="App",
                verify_condition=VerifyCondition(strategy="visual", description="OK"),
            )
            for i in range(5)
        ])
        report = await runner.run(skill, mode=RunMode.ASSISTED)

        # With alternating: step 0 fails (total=1, consec=1), step 1 passes (consec=0),
        # step 2 fails (total=2, consec=1), step 3 passes (consec=0),
        # step 4 fails (total=3, consec=1) → total_failures >= 3 → escalate
        assert not report.passed
        # Human should have been asked for per-step escalation (steps 0, 2, 4)
        # plus the full-skill escalation
        assert len(human_calls) >= 3
        # Total failures reached threshold 3, triggering escalation
        assert len(escalate_calls) >= 1


# ═══════════════════════════════════════════════════════════
# Gap 7: SessionResult validation fields
# ═══════════════════════════════════════════════════════════


class TestSessionResultValidation:
    """Test that SessionResult carries validation state."""

    def test_session_result_has_validation_fields(self):
        from protean.realtime.session import SessionResult
        from protean.realtime.transcript import Transcript

        result = SessionResult(transcript=Transcript())
        assert result.validation_passed is None
        assert result.validation_duration == 0.0

    def test_session_result_with_validation(self):
        from protean.realtime.session import SessionResult
        from protean.realtime.transcript import Transcript

        result = SessionResult(
            transcript=Transcript(),
            validation_passed=True,
            validation_duration=3.5,
        )
        assert result.validation_passed is True
        assert result.validation_duration == 3.5

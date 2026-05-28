"""Execution telemetry — structured JSONL logging for skill runs.

Every skill execution (validate or assisted) produces a telemetry entry
with per-step timing, verification strategy used, retry counts, and
overall outcome. This data feeds V2's path optimizer.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protean.skills.runner import RunReport

log = logging.getLogger(__name__)


@dataclass
class StepTelemetry:
    """Telemetry for a single step execution."""
    step_index: int
    action: str = ""
    verify_strategy: str = ""   # "ax_element", "text_content", "visual", "none", "end_state"
    verify_result: str = ""     # "passed", "failed", "skipped", "timeout"
    strategy_used: str = ""     # actual strategy that produced the result (after fallback)
    attempts: int = 1
    duration_ms: int = 0
    screenshot_path: str = ""


@dataclass
class RunTelemetry:
    """Telemetry for an entire skill run."""
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    skill_name: str = ""
    timestamp: str = ""         # ISO 8601
    mode: str = ""              # "validate" or "assisted"
    execution_mode: str = ""    # "full" or "step_by_step"
    steps: list[StepTelemetry] = field(default_factory=list)
    total_duration_ms: int = 0
    outcome: str = ""           # "passed", "failed", "escalated", "timeout", "error"
    failure_step: int | None = None
    failure_reason: str = ""
    llm_calls: int = 0         # total LLM verification calls made

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line."""
        data = asdict(self)
        return json.dumps(data, ensure_ascii=False)


class TelemetryLogger:
    """Writes run telemetry to a JSONL file.

    Usage:
        logger = TelemetryLogger(data_dir / "telemetry")
        logger.log(run_telemetry)
    """

    def __init__(self, telemetry_dir: Path) -> None:
        self._dir = telemetry_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def log_path(self) -> Path:
        """Path to the telemetry JSONL file."""
        return self._dir / "runs.jsonl"

    def log(self, entry: RunTelemetry) -> None:
        """Append a run telemetry entry to the log file."""
        line = entry.to_jsonl()
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            log.warning("Failed to write telemetry: %s", self.log_path, exc_info=True)

    def from_report(
        self,
        report: RunReport,
        *,
        execution_mode: str = "full",
        step_telemetry: list[StepTelemetry] | None = None,
    ) -> RunTelemetry:
        """Convert a RunReport into a RunTelemetry entry.

        If step_telemetry is provided, it's used directly.
        Otherwise, minimal entries are created from report.steps.
        """
        from datetime import datetime, timezone

        steps = step_telemetry or [
            StepTelemetry(
                step_index=sv.index,
                verify_result=sv.result.value,
                attempts=sv.attempts,
            )
            for sv in report.steps
        ]

        aborted = report.aborted_at
        return RunTelemetry(
            skill_name=report.skill_name,
            timestamp=datetime.now(timezone.utc).isoformat(),
            mode=report.mode.value,
            execution_mode=execution_mode,
            steps=steps,
            total_duration_ms=int(report.duration * 1000),
            outcome="passed" if report.passed else "failed",
            failure_step=aborted,
            failure_reason=(
                report.steps[aborted].reason
                if aborted is not None and aborted < len(report.steps)
                else ""
            ),
        )

    def read_runs(self, skill_name: str | None = None, limit: int = 100) -> list[RunTelemetry]:
        """Read recent telemetry entries, optionally filtered by skill name."""
        entries: list[RunTelemetry] = []
        if not self.log_path.exists():
            return entries
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if skill_name and data.get("skill_name") != skill_name:
                            continue
                        step_data = data.pop("steps", [])
                        entry = RunTelemetry(**{k: v for k, v in data.items() if k != "steps"})
                        entry.steps = [StepTelemetry(**s) for s in step_data]
                        entries.append(entry)
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            log.warning("Failed to read telemetry: %s", self.log_path, exc_info=True)
        return entries[-limit:]

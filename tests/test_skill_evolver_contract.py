from __future__ import annotations

from protean.skills.builder import SkillBuilder
from protean.skills.evolve import EvolveAction, SkillEvolver
from protean.skills.runner import RunTrajectory, StepTrajectory
from protean.skills.schema import Skill, Step


def test_evolve_action_guidance_is_focused_learning_contract(tmp_path):
    action = EvolveAction(
        action="refine",
        skill_name="mac-outlook-event-creation",
        reason="The run exposed a missing autocomplete detail.",
        intent="Make room autocomplete selection explicit before sending.",
        observed_gap="The existing room step treats the field as plain text.",
        evidence=[
            "The agent typed the room code into Outlook's location field.",
            "Outlook showed a Conf Rm BJW suggestion that had to be selected.",
        ],
    )

    guidance = SkillEvolver(tmp_path, llm=object())._format_action_guidance(action)

    assert "Action: refine" in guidance
    assert "Intent: Make room autocomplete selection explicit" in guidance
    assert "Observed gap: The existing room step treats" in guidance
    assert "- Outlook showed a Conf Rm BJW suggestion" in guidance


class _FakeCreateLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_structured(self, messages, *, response_model, model, temperature):
        self.calls += 1
        name = "outlook-booking" if self.calls == 1 else "computer-use-session-lifecycle"
        return Skill(
            name=name,
            description="Keep Computer Use sessions valid before desktop actions.",
            when_to_use=["Before using Computer Use after a pause or app switch."],
            when_not_to_use=["When no desktop GUI interaction is needed."],
            goal="Refresh app state before GUI actions that depend on a live session.",
            steps=[Step(
                name="refresh-app-state",
                action="Call get_app_state for the target app before GUI actions.",
            )],
            success_criteria=["GUI actions run against a fresh app state."],
        ), None


async def test_create_from_trajectory_retries_name_body_contract():
    llm = _FakeCreateLLM()
    trajectory = RunTrajectory(
        skill_name="codex-session",
        task="Reserve an Outlook meeting.",
        steps=[StepTrajectory(
            step_index=-1,
            step_name="full-execution",
            instruction="Reserve an Outlook meeting.",
        )],
    )

    skill = await SkillBuilder().from_trajectory(
        trajectory,
        llm,
        target_name="computer-use-session-lifecycle",
        evolution_guidance=(
            "Intent: Maintain a valid Computer Use session before GUI actions."
        ),
    )

    assert llm.calls == 2
    assert skill.name == "computer-use-session-lifecycle"

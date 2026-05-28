"""Prompt templates used by the skill builder.

One prompt per module, kept as plain strings so they can be diffed and edited
without touching builder logic.
"""

from protean.skills.prompts.create_from_trajectory import CREATE_FROM_TRAJECTORY_PROMPT
from protean.skills.prompts.finalize import FINALIZE_PROMPT
from protean.skills.prompts.output_hotspot_hint import OUTPUT_HOTSPOT_HINT_PROMPT
from protean.skills.prompts.recording import RECORDING_SYSTEM_PROMPT
from protean.skills.prompts.refine import REFINE_PROMPT
from protean.skills.prompts.step_field_rules import STEP_FIELD_RULES

__all__ = [
    "CREATE_FROM_TRAJECTORY_PROMPT",
    "FINALIZE_PROMPT",
    "OUTPUT_HOTSPOT_HINT_PROMPT",
    "RECORDING_SYSTEM_PROMPT",
    "REFINE_PROMPT",
    "STEP_FIELD_RULES",
]

"""Advisory prompt block flagging the transcript's largest outputs.

Placeholders (substituted via ``str.replace``):
- ``{total_raw}``       — total raw chars, pre-formatted with thousands separator.
- ``{total_rendered}``  — total rendered chars, pre-formatted similarly.
- ``{step_block}``      — newline-joined "- Step N ..." bullets.
"""

OUTPUT_HOTSPOT_HINT_PROMPT = """\
## Skill-output cost hotspots (advisory)

Trajectory totals: ~{total_raw} raw chars of tool output / agent text; ~{total_rendered} chars after head+tail truncation in the transcript above. The `run_script` bodies and `run_terminal_command` strings embedded in the skill you are writing now are *your* output — on every future run of this skill, the executor sees exactly what you choose to print there, and every char of it becomes input tokens. That makes shaping it your job, not a runtime concern.

Heaviest steps (raw chars, biggest first):
{step_block}

### How to use this — performance comes first

Reliability is non-negotiable. Never drop data the skill, verifier, or a downstream step depends on just to save tokens. Procedure:

1. For each heavy step above, identify the exact fields / lines / values the executor or a later step used (to decide, fill a parameter, or satisfy a verify_condition). That signal set must be preserved.
2. For each heavy step driven by a `run_script` or `run_terminal_command` in this skill, do the full read / parse inside the script and print only the bytes a downstream consumer actually reads: the chosen value, the matched line, the relevant fields, the changed rows. Any reshaping is fair game (filter, grep, slice, JSON, head / tail, aggregate, summary) as long as the consumed signal survives intact at the precision the consumer needs. The mistake to avoid is reshaping output *before* you have identified what downstream reads from it — that's how blind truncation chops signal and lossy summaries erase details a later step actually needed.
3. Sanity check: re-read every step, parameter, verify_condition, and branch — confirm the leaner output still supports each decision.
""" # noqa: E501

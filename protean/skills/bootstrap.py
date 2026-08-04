"""Built-in bootstrap skills shipped with Protean."""

# ruff: noqa: E501

from __future__ import annotations

from textwrap import dedent

from protean.executor.providers.prompts.batching import BATCHING_GUIDANCE
from protean.skills.schema import Skill, SkillParameter, Step

AGENT_PROTEAN_SKILL_NAME = "build-and-evolve-skills-with-protean"


def _terminal_tool(command: str) -> str:
    return f"run_terminal_command(command={command!r})"


def build_and_evolve_skills_with_protean_skill(
    *,
    source_default: str = "codex",
) -> Skill:
    """Return the agent skill for using Protean's skill lifecycle.

    The skill is rendered through Protean's normal renderer before being copied
    into Codex, Claude Code, or another agent runtime skill directory.
    """
    source_var = "{{source}}"
    label_var = "{{label}}"
    task_var = "{{task}}"
    session_var = "{{session}}"
    mark_start = (
        f'protean trajectories mark start '
        f'--source "{source_var}" --label "{label_var}" --task "{task_var}" '
        f'--session "{session_var}"'
    )
    mark_end = (
        f'protean trajectories mark end '
        f'--source "{source_var}" --label "{label_var}" --session "{session_var}"'
    )
    inspect_and_evolve = (
        f'protean trajectories inspect '
        f'--source "{source_var}" --label "{label_var}" --session "{session_var}" && '
        'protean trajectories evolve '
        f'--source "{source_var}" --label "{label_var}" --session "{session_var}" '
        f'--task "{task_var}"'
    )
    inspect_from_message = (
        f'protean trajectories inspect '
        f'--source "{source_var}" --session "{session_var}" '
        '--from-message "<begin user message>" --to-message "<optional end message>"'
    )
    evolve_from_message = (
        f'protean trajectories evolve '
        f'--source "{source_var}" --session "{session_var}" '
        '--from-message "<begin user message>" --to-message "<optional end message>" '
        f'--task "{task_var}"'
    )
    operating_guide = dedent(
        f"""
        Use Protean as the local capability factory for operational skills. Protean
        converts demonstrations, realtime teaching sessions, zero-shot runs,
        hand-written `SKILL.md` folders, imported skill libraries, replay refinement,
        and current agent trajectories into reusable skills that can be replayed,
        validated, refined, and shared.

        Workflow choice:

        - Recorded screen demonstration: when the user will demonstrate on screen,
          record the demonstration, then run `generate` with a task description.
        - Realtime voice or screen-share session: when the user wants live teaching
          or hotkey capture, run `daemon`.
        - Zero-shot task prompt: when the user wants a one-off task, use
          `skills run -t`.
        - Hand-edit a SKILL.md: when the user wants to manually maintain a skill,
          edit the relevant `SKILL.md`, then list, show, or run it.
        - Import another skill library: when the user has a skill from another
          deployment, copy the skill directory into the skills directory, then list
          or run it.
        - Agent self-refinement during execution: when an existing skill should
          improve after replay, run it with `--refine`.
        - Current-session trajectory evolution: when this agent's own work should
          become reusable operational knowledge, mark, inspect, and evolve the
          current local runtime trajectory.

        Useful commands:

        ```bash
        protean --help
        protean skills list
        protean skills show SKILL_NAME
        protean skills run SKILL_NAME
        protean skills run SKILL_NAME --refine
        protean skills run -t "Open Calculator and compute 1+1"
        protean record -o ./recordings/my-task
        protean generate ./recordings/my-task -d "Describe the reusable task"
        protean daemon
        ```

        Current-session trajectory evolution commands:

        ```bash
        protean trajectories mark start \\
          --source "{source_var}" --label "{label_var}" --task "{task_var}" \\
          --session "{session_var}"
        protean trajectories mark end \\
          --source "{source_var}" --label "{label_var}" --session "{session_var}"
        protean trajectories inspect \\
          --source "{source_var}" --label "{label_var}" --session "{session_var}"
        protean trajectories evolve \\
          --source "{source_var}" --label "{label_var}" --session "{session_var}" \\
          --task "{task_var}"
        ```
        """
    ).strip()
    operating_rules = dedent(
        """
        Follow these operating rules while using Protean:

        - Inspect the current skill or recording before running irreversible workflows.
        - Treat GUI actions that send messages, submit forms, delete data, book rooms,
          or invite attendees as irreversible; ask before the final action unless the
          user clearly requested that exact side effect.
        - Pick the right `skills run` execution mode:
          * `-s` / `--stepwise` to execute and verify one step at a time when a skill
            has fragile UI or you want to catch failures early.
          * `-a` / `--assist` to let a failing step ask the user back through the
            attached AssistantChannel. Only useful when a channel is attached.
          * `--refine` to feed the run trajectory back into SkillBuilder.refine() so
            the skill improves after replay.
        """
    ).strip()
    platform_tools = dedent(
      f"""
        Tool surfaces available alongside your runtime's native tools:

        - **Protean MCP** (`mcp__protean__*`): coordinate-driven desktop control
          backed by the OS's native input + screen capture. Works for any GUI
          including apps without an AppleScript / Automation API.

        - **Protean CLI** (`protean ...` via your shell tool): skill and
          trajectory lifecycle that has no other entry point — `skills list /
          show / run`, `record`, `generate`, `daemon`, `trajectories
          mark / inspect / evolve`.

        - **Batching policy for `mcp__protean__*`**:
          For MCP routes, pass `include_screenshot: false` on every call in the
          batch except the last call; only the final action should return the
          screenshot that confirms outcome.

        {BATCHING_GUIDANCE}
        """
    ).strip()

    return Skill(
        name=AGENT_PROTEAN_SKILL_NAME,
        description="Use Protean to build, run, validate, import, hand-edit, refine, and evolve reusable operational skills, including evolving the current agent trajectory when appropriate.",
        when_to_use=[
            "The user asks to use Protean, create a skill, run a skill, validate a skill, refine a skill, inspect the skill library, or turn work into reusable operational knowledge.",
            "The user demonstrates or describes a workflow that should become a reusable Protean skill.",
            "The user wants to record a screen demonstration, use realtime teaching, run a zero-shot task, hand-edit or import a SKILL.md, or refine an existing skill.",
            "A task is likely to produce reusable operational knowledge: GUI work, multi-step tool use, environment setup, workflow debugging, or a process that another agent should be able to repeat later.",
            "The current agent task may itself become reusable operational knowledge and should be marked for later trajectory evolution.",
        ],
        when_not_to_use=[
            "The task is only casual conversation or a one-line factual answer.",
            "The user wants current-session trajectory evolution, but the agent runtime cannot provide or identify its current trajectory/session.",
        ],
        goal=dedent(
            """\
            Choose the right Protean entry channel for reusable operational knowledge.
            Use recorded demonstrations, realtime teaching, zero-shot execution, manual
            SKILL.md editing, imports, replay refinement, or current-session trajectory
            evolution according to how the capability is arriving. When the current agent
            session is the source, the local session transcript is the authoritative
            trajectory: user messages, assistant messages, tool calls, and tool results are
            imported from the runtime's local session file.
            """
        ).strip(),
        parameters=[
            SkillParameter(
                name="source",
                description="Agent trajectory source, such as codex or claude_code.",
                default=source_default,
                required=False,
            ),
            SkillParameter(
                name="label",
                description="Short label for the trajectory episode when evolving a session.",
                required=False,
            ),
            SkillParameter(
                name="task",
                description="The user task that should be learned from when evolving a session.",
                required=False,
            ),
            SkillParameter(
                name="session",
                description="Current runtime session path or identifier when the runtime exposes it.",
                default="current",
                required=False,
            ),
        ],
        steps=[
            Step(
                name="use-protean-operating-guide",
                action=operating_guide,
            ),
            Step(
                name="follow-operating-rules",
                action=operating_rules,
            ),
            Step(
                name="know-protean-tool-surfaces",
                action=platform_tools,
            ),
            Step(
                name="mark-begin-early",
                action=dedent(
                    """\
                    For current-session trajectory evolution, pick a short stable label for
                    this one episode and use exactly one current runtime session; do not
                    merge sessions. Mark begin as early as practical: after the user task is
                    clear and before the first meaningful action that should be learned,
                    especially before the first GUI/tool call that changes state or
                    discovers key context. If the user is still clarifying requirements,
                    wait until the reusable task boundary is clear, then mark immediately.
                    If you realize late that the task should be captured, mark now and later
                    use an explicit message/time range for the missed prefix instead of
                    pretending the marker was earlier. If the runtime knows its current
                    session file, pass it explicitly; otherwise use session=current.
                    """
                ).strip(),
                tool=_terminal_tool(mark_start),
            ),
            Step(
                name="execute-user-task",
                action=dedent(
                    """\
                    Execute the selected Protean workflow or the user's task normally. For
                    current-session trajectory evolution, keep the episode open through
                    approvals, corrections, verification, and final irreversible actions.
                    The runtime's own trajectory is the authoritative record. The expected
                    imported shape is a simple ReAct stream: messages with role/text, tool
                    calls with tool name, arguments, and call id, and tool results with the
                    matching call id. Do not try to summarize away user corrections; they
                    are useful refinement signal.

                    Treat GUI actions that send messages, submit forms, delete data, book
                    rooms, or invite attendees as irreversible; ask before the final action
                    unless the user has clearly requested that exact side effect.
                    """
                ).strip(),
            ),
            Step(
                name="mark-end-late",
                action=dedent(
                    """\
                    For current-session trajectory evolution, mark end late, after the task
                    reaches a stable stopping point: the result is verified, the user
                    accepts it, the user stops giving corrections, or the user explicitly
                    asks to evolve what just happened. If the agent thinks it is finished
                    but the user gives advice or correction afterward, keep the episode
                    open and mark end after that correction is handled. If end was already
                    marked too early, tolerate it: treat the later correction as a separate
                    refinement episode rather than rewriting history.
                    """
                ).strip(),
                tool=_terminal_tool(mark_end),
            ),
            Step(
                name="inspect-and-evolve",
                action=dedent(
                    f"""\
                    For current-session trajectory evolution, inspect the selected
                    trajectory slice before evolving. Confirm it is one session, includes
                    the user's task request, includes the meaningful tool calls and tool
                    results, and ends after verification or final correction. If Protean
                    cannot find the marker range, or if begin/end were missed, assign an
                    explicit range from session messages or timestamps and retry; if
                    Protean rejects that range, regenerate the begin/end selection rather
                    than guessing. Prefer a slightly redundant slice over a too-short one;
                    Protean can tolerate extra context better than missing the task
                    boundary. When the slice is acceptable, evolve it through Protean's
                    SkillEvolver. Do not replay or validate skills with real-world side
                    effects unless the user explicitly approves that run.

                    `evolve` needs to analyze the trajectory and refine skills. Since this
                    can take some time, run it detached and avoid blocking the agent.

                    Recovery commands for an explicit message range are:
                    `{inspect_from_message}` then `{evolve_from_message}`.
                    """
                ).strip(),
                tool=_terminal_tool(inspect_and_evolve),
                idempotent=False,
            ),
        ],
        success_criteria=[
            "The agent chooses the Protean entry channel that matches how capability arrives.",
            "The agent records, generates, runs, validates, imports, hand-edits, refines, or evolves skills through Protean when requested.",
            "Current-session trajectory evolution captures only reusable operational work.",
            "The begin boundary is before the first meaningful reusable action when practical.",
            "The end boundary is after verification, user acceptance, or final correction.",
            "If markers are missed, the agent assigns a clear explicit message/time range and lets Protean reject invalid ranges.",
            "Protean evolves only one current-session slice; it does not merge multiple sessions.",
            "The skill remains usable by Codex, Claude Code, and future agents.",
        ],
        tags=["protean", "skill-lifecycle", "trajectory", "agent-runtime", "evolution"],
        source="builtin",
        metadata={"bootstrap": True},
    )

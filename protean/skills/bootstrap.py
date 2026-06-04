"""Built-in bootstrap skills shipped with Protean."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from protean.skills.schema import Skill, SkillParameter, Step

AGENT_PROTEAN_SKILL_NAME = "build-and-evolve-skills-with-protean"


def _terminal_tool(command: str) -> str:
    return f"run_terminal_command(command={command!r})"


def _repo_root_text(protean_root: str | Path | None) -> str:
    if not protean_root:
        return "<Protean repo root>"
    return str(Path(protean_root).expanduser())


def build_and_evolve_skills_with_protean_skill(
    *,
    source_default: str = "codex",
    protean_root: str | Path | None = None,
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
    repo_root = _repo_root_text(protean_root)
    operating_guide = dedent(
        f"""
        Use Protean as the local capability factory for operational skills. Protean
        converts demonstrations, realtime teaching sessions, zero-shot runs,
        hand-written `SKILL.md` folders, imported skill libraries, replay refinement,
        and current agent trajectories into reusable skills that can be replayed,
        validated, refined, and shared.

        Local assumptions:

        - Repo root: `{repo_root}` (skills/recordings/bridge live here).
        - Skills live under `data/skills` unless `PROTEAN_SKILLS_DIR` overrides it.
        - Recordings live under `data/recordings` unless `PROTEAN_RECORDINGS_DIR`
          overrides it.
        - The realtime bridge lives in `electron-bridge/`; use npm only inside that
          subproject.

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

        - If the user only wants to equip this runtime, run
          `protean agents setup <target>`; setup is not the same as skill use.
        - Inspect the current skill or recording before running irreversible workflows.
        - Treat GUI actions that send messages, submit forms, delete data, book rooms,
          or invite attendees as irreversible; ask before the final action unless the
          user clearly requested that exact side effect.
        - Do not capture casual conversation, one-line factual answers, or pure writing
          unless the user explicitly wants the procedure learned.
        - If the task contains private, credential-like, or sensitive material, ask for
          consent before recording, marking, or evolving.
        - Keep trajectory evolution based on the local runtime session transcript, not
          platform tool hooks.
        - Prefer low-risk zero-shot tasks or validation-only runs when checking setup.
        - Use `-s` for stepwise validation when a skill has fragile UI steps.
        - Use `-a` only when an attached assistant channel should help recover failed
          steps.
        """
    ).strip()
    platform_tools = dedent(
        """
        Tool routing — use your runtime's native tools as the primary surface.
        Protean is a skill / trajectory factory; it adds capability where the
        runtime has none.

        For every action, pick the tool in this order:

        1. **A scripted or CLI route** (a shell command, a script, a keyboard
           shortcut, a native API call) that reaches the same end state. This
           is the most deterministic and the fastest, regardless of surface.
        2. **The runtime's native tool** that matches the action — a built-in
           or bundled tool for GUI control, browser navigation, shell
           execution, file read/write, or search. Use whatever the runtime
           gives you natively; it is the contract that runtime is built
           around.
        3. **Protean MCP**, used for two distinct purposes:
           - **Skill / trajectory lifecycle** — discovery, show, run, mark,
             inspect, evolve via `protean skills ...` and
             `protean trajectories ...`. This work has no native
             equivalent and always belongs to Protean.
           - **GUI fallback** — Protean's accessibility-aware tools
             (`activate_app`, `find_elements`, `click_at`, `type_text`,
             `menu_click`, `select_option`, …) when the runtime has no
             native GUI surface, or when the native GUI tool has failed on
             a specific element and you need Protean's AX layer to recover.
        """
    ).strip()

    return Skill(
        name=AGENT_PROTEAN_SKILL_NAME,
        description=(
            "Use Protean to build, run, validate, import, hand-edit, refine, and evolve "
            "reusable operational skills, including evolving the current agent trajectory "
            "when appropriate."
        ),
        when_to_use=[
            "The user asks to use Protean, create a skill, run a skill, validate a skill, "
            "refine a skill, inspect the skill library, or turn work into reusable "
            "operational knowledge.",
            "The user demonstrates or describes a workflow that should become a reusable "
            "Protean skill.",
            "The user wants to record a screen demonstration, use realtime teaching, run a "
            "zero-shot task, hand-edit or import a SKILL.md, or refine an existing skill.",
            "A task is likely to produce reusable operational knowledge: GUI work, "
            "multi-step tool use, environment setup, workflow debugging, or a process that "
            "another agent should be able to repeat later.",
            "The current agent task may itself become reusable operational knowledge and "
            "should be marked for later trajectory evolution.",
        ],
        when_not_to_use=[
            "The task is only casual conversation or a one-line factual answer.",
            "The user only wants to install or copy Protean skills into an agent runtime; use "
            "`protean agents setup <target>` for setup instead.",
            "The task contains sensitive or private material and the user has not opted in.",
            "The user wants current-session trajectory evolution, but the agent runtime cannot "
            "provide or identify its current trajectory/session.",
        ],
        goal=(
            "Choose the right Protean entry channel for reusable operational knowledge. "
            "Use recorded demonstrations, realtime teaching, zero-shot execution, manual "
            "SKILL.md editing, imports, replay refinement, or current-session trajectory "
            "evolution according to how the capability is arriving. When the current agent "
            "session is the source, the local session transcript is the authoritative "
            "trajectory: user messages, assistant messages, tool calls, and tool results are "
            "imported from the runtime's local session file."
        ),
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
                description=(
                    "Current runtime session path or identifier when the runtime exposes it."
                ),
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
                name="prefer-runtime-native-gui-tools",
                action=platform_tools,
            ),
            Step(
                name="mark-begin-early",
                action=(
                    "For current-session trajectory evolution, pick a short stable label for "
                    "this one episode and use exactly one current runtime session; do not merge "
                    "sessions. Mark begin as early as practical: after the user task is clear and "
                    "before the first meaningful action that should be learned, especially "
                    "before the first GUI/tool call that changes state or discovers key context. "
                    "If the user is still clarifying requirements, wait until the reusable task "
                    "boundary is clear, then mark immediately. If you realize late that the task "
                    "should be captured, mark now and later use an explicit message/time range "
                    "for the missed prefix instead of pretending the marker was earlier. If the "
                    "runtime knows its current session file, pass it explicitly; otherwise use "
                    "session=current."
                ),
                tool=_terminal_tool(mark_start),
            ),
            Step(
                name="execute-user-task",
                action=(
                    "Execute the selected Protean workflow or the user's task normally. For "
                    "current-session trajectory evolution, keep the episode open through "
                    "approvals, corrections, verification, and final irreversible actions. The "
                    "runtime's own trajectory is the authoritative record. The expected imported "
                    "shape is a simple ReAct stream: messages with role/text, tool calls with "
                    "tool name, arguments, and call id, and tool results with the matching call "
                    "id. Do not try to summarize away user corrections; they are useful "
                    "refinement signal. "
                    "When operating desktop apps, prefer accessibility discovery such as "
                    "find/list elements before coordinate clicks when labels are available. "
                    "Treat GUI actions that send messages, submit forms, delete data, book rooms, "
                    "or invite attendees as irreversible; ask before the final action unless the "
                    "user has clearly requested that exact side effect."
                ),
            ),
            Step(
                name="mark-end-late",
                action=(
                    "For current-session trajectory evolution, mark end late, after the task "
                    "reaches a stable stopping point: the result is verified, the user accepts "
                    "it, the user stops giving corrections, or the user explicitly asks to "
                    "evolve what just happened. If the agent thinks it is finished but the user "
                    "gives advice or correction afterward, keep the episode open and mark end "
                    "after that correction is handled. If end was already marked too early, "
                    "tolerate it: treat the later correction as a separate refinement episode "
                    "rather than rewriting history."
                ),
                tool=_terminal_tool(mark_end),
            ),
            Step(
                name="inspect-and-evolve",
                action=(
                    "For current-session trajectory evolution, inspect the selected trajectory "
                    "slice before evolving. Confirm it is one session, includes the user's task "
                    "request, includes the meaningful tool calls and tool results, and ends after "
                    "verification or final correction. If Protean cannot find the marker range, "
                    "or if begin/end were missed, assign an explicit range from session messages "
                    "or timestamps and retry; if Protean rejects that range, regenerate the "
                    "begin/end selection rather than guessing. Prefer a slightly redundant slice "
                    "over a too-short one; Protean can tolerate extra context better than missing "
                    "the task boundary. When the slice is acceptable, evolve it through Protean's "
                    "SkillEvolver. Do not replay or validate skills with real-world side effects "
                    "unless the user explicitly approves that run. "
                    "Recovery commands for an explicit "
                    f"message range are: `{inspect_from_message}` then `{evolve_from_message}`."
                ),
                tool=_terminal_tool(inspect_and_evolve),
                idempotent=False,
            ),
        ],
        success_criteria=[
            "The agent chooses the Protean entry channel that matches how capability arrives.",
            "The agent records, generates, runs, validates, imports, hand-edits, refines, or "
            "evolves skills through Protean when requested.",
            "Current-session trajectory evolution captures only reusable operational work, "
            "with consent for sensitive tasks.",
            "The begin boundary is before the first meaningful reusable action when practical.",
            "The end boundary is after verification, user acceptance, or final correction.",
            "If markers are missed, the agent assigns a clear explicit message/time range and "
            "lets Protean reject invalid ranges.",
            "Protean evolves only one current-session slice; it does not merge multiple sessions.",
            "The skill remains usable by Codex, Claude Code, and future agents.",
        ],
        tags=["protean", "skill-lifecycle", "trajectory", "agent-runtime", "evolution"],
        source="builtin",
        metadata={"bootstrap": True},
    )

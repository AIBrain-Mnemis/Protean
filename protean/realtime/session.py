"""Teach session — core realtime teaching loop.

Orchestrates a complete teaching conversation through the Electron bridge:
  1. Open a bridge session (system_instruction + tools + accept_call_id)
  2. Audio + screen capture happen inside the bridge renderer (we never see PCM)
  3. Handle Python-routed tool calls: observe_step, execute_step, revise, finalize
  4. Maintain SkillBuilder throughout the conversation
  5. On finalize or disconnect: produce Skill

Bridge-routed tools (start_screen / stop_screen / request_screenshot) are
handled inside the bridge and never reach this class.

All transport (TRTC + realtime LLM + presence) lives in the bridge renderer;
this module owns only the talker loop, the skill builder, and the local
executor.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from protean.channels._protocol import SessionStartPayload
from protean.channels.realtime_events import RealtimeEventType
from protean.channels.realtime_llm_bridge import BridgeBackedRealtimeLLM
from protean.executor import ExecutorContext
from protean.realtime.transcript import Transcript
from protean.skills.builder import SkillBuilder

if TYPE_CHECKING:
    from protean.executor import ExecutorEvent, ExecutorProvider
    from protean.llm import LLM
    from protean.platform.base import Platform
    from protean.skills.schema import Skill

log = logging.getLogger(__name__)


_SYSTEM_INSTRUCTION = """\
You are Protean, a voice-based desktop assistant.
You watch the user's screen, talk with them, and learn their tasks.
You coordinate but never operate the computer directly.

Keep responses to one or two sentences. Speak like a colleague.
Respond in the user's language.
When the user describes their goal, call set_task.

## Screen modes

Call start_screen to begin receiving screenshots.
Carefully understand whether the user wants to show you or watch you before choosing a mode.

- mode='observe': the user shares their screen for you to watch. You see their actions and record steps via observe_step. Do NOT call execute_step while observing. Trigger phrases: "let me show you", "watch my screen", "I'll demonstrate"
- mode='share': you share your screen for the user to watch. Used when you execute steps and the user wants to see. Trigger phrases: "show me how", "demonstrate it", "let me watch you"

Only one mode can be active. Call stop_screen to end the current mode.

## execute_step rules

- user_request = the user's spoken request, verbatim. Don't paraphrase or change the verb (click stays click; open stays open).
- Preserve surrounding context the user gave (prior requests, constraints, clarifications, corrections) when it's needed to make the request unambiguous.
- A short reply, question, or referring expression is not self-contained. Quote your previous suggestion or the prior turn so the executor knows the referent.
- Use on-screen UI labels verbatim; never translate.
- The executor reads the screen itself; you don't need to describe UI moves.

## observe_step rules

- Keep using intent + action describing what the user just did.

## Executor concurrency (one at a time)

- One execute_step / replay_skill runs at a time; consecutive calls share executor state.
- "Executor is busy" → tell user you'll do it after; don't retry.
- During execution, on user speech:
  - stop/wait/cancel → interrupt_executor
  - direction change → interrupt_executor, then new execute_step
  - progress check → check_executor (no interrupt)
  - chitchat / unrelated → answer briefly, don't interrupt
  - follow-up step → ack verbally; execute_step after the current done

## Replay

Call replay_skill only after a skill has been finalized.
Always provide the finalized skill name explicitly.
Replay loads that finalized skill as the current editable draft, resets the executor session, then sends the whole rendered skill to the executor for end-to-end execution.
If the workflow is still immature, use execute_step instead of replay_skill.
Start mode='share' so the user can watch.

## Feedback

Call revise_step or remove_step to update the draft.

## Done

Call finalize_skill. Briefly summarize what was learned.

## Screenshots

Only arrive after start_screen. ~1 per second.
Reference elements by visible label, not position.
"""  # noqa: E501

# Hidden user turn sent right after session_start so Gemini speaks first
# (Live only responds to input). Bracketed tag = bookkeeping, not user.
_GREETING_PROMPT = (
    "[system] Connected. Greet the user in their language in one short sentence. Don't introduce yourself or list features."  # noqa: E501
)

# How many of the most recent user/assistant voice turns to inject as
# context when the talker calls execute_step. 5 rounds = up to 10 entries.
_EXECUTE_STEP_TRANSCRIPTION_ROUNDS = 10


@dataclass
class SessionResult:
    """Result of a completed teach session."""

    transcript: Transcript
    skill: Skill | None = None
    skill_path: Path | None = None
    duration: float = 0.0
    error: str | None = None
    validation_passed: bool | None = None  # None = not validated
    validation_duration: float = 0.0


@dataclass
class ToolResult:
    """Result from a realtime tool call."""

    message: str
    is_async: bool = False  # True if the tool started a background task


def build_session_start_payload(
    accept_call_id: str,
    *,
    voice: str = "Puck",
    input_sample_rate: int = 16000,
    output_sample_rate: int = 24000,
    video_fps_limit: int = 2,
) -> SessionStartPayload:
    """Build the SessionStartPayload that TeachSession sends.

    Bridge owns provider/api_key/model — they don't appear here. Per the
    ownership invariant, Python owns system_instruction +
    tools + handler-routing.
    """
    from protean.realtime.tools import build_realtime_tool_declarations

    return {
        "realtime": {
            "voice": voice,
            "input_sample_rate": input_sample_rate,
            "output_sample_rate": output_sample_rate,
            "video_fps_limit": video_fps_limit,
            "system_instruction": _SYSTEM_INSTRUCTION,
            "tools": build_realtime_tool_declarations(),  # type: ignore[typeddict-item]
        },
        "accept_call_id": accept_call_id,
    }


class TeachSession:
    """A single realtime teaching session, backed by the Electron bridge."""

    def __init__(
        self,
        realtime_client: BridgeBackedRealtimeLLM,
        platform: Platform,
        skill_llm: LLM,
        executor: ExecutorProvider,
        accept_call_id: str,
        executor_event_observer: Callable[["ExecutorEvent"], None] | None = None,
        talker_tool_observer: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._client = realtime_client
        self._platform = platform
        self._skill_llm = skill_llm
        self._executor = executor
        self._accept_call_id = accept_call_id
        self._executor_event_observer = executor_event_observer
        self._talker_tool_observer = talker_tool_observer
        self._transcript = Transcript()
        self._builder = SkillBuilder()
        self._executor_task: asyncio.Task | None = None
        self._finalize_task: asyncio.Task | None = None
        self._stop_requested = False
        self._skills_by_name: dict[str, Skill] = {}
        self._saved_skill_paths_by_name: dict[str, Path] = {}
        self._stop_reason = ""
        self._executor_started = False
        self._task = ""
        self._executor_messages: list[tuple[float, str]] = []
        self._last_validation_passed: bool | None = None
        self._last_validation_duration: float = 0.0

        # Tracks the bridge's current screen-capture target so the executor
        # (CUA) can be told which surface the human user is actually viewing.
        # Updated by `screen_state` events from the bridge; consumed in
        # `_build_executor_context` and `_handle_screen_state_change`.
        self._active_share_mode: str = "off"
        self._active_share_label: str = ""
        # 1-based physical display index the bridge is sharing. None when no
        # active share OR the bridge didn't report one (observe mode). The
        # executor uses this to target the same display via its MCP tools.
        self._active_share_display: int | None = None

        # Bridge-routed tools (start_screen / stop_screen / request_screenshot)
        # are handled inside the bridge per the `handler:"bridge"` annotation
        # on those tool declarations. They never reach this dispatch table.
        self._tool_handlers: dict[str, Callable[[dict], Awaitable[ToolResult]]] = {
            # Skill editing
            "observe_step": self._tool_observe_step,
            "revise_step": self._tool_revise_step,
            "remove_step": self._tool_remove_step,
            "finalize_skill": self._tool_finalize_skill,
            # Execution
            "execute_step": self._tool_execute_step,
            "replay_skill": self._tool_replay_skill,
            "interrupt_executor": self._tool_interrupt_executor,
            "check_executor": self._tool_check_executor,
            # Session
            "set_task": self._tool_set_task,
        }

    def request_stop(self, reason: str = "external") -> None:
        """Ask the run loop to wind down at the next event boundary."""
        self._stop_requested = True
        self._stop_reason = reason

    async def run(self) -> SessionResult:
        """Main loop: open bridge session → handle events → build skill."""
        # Accumulator for streaming assistant text. Gemini delivers the
        # output transcript as small chunks; we collect them and only log
        # one full line per turn, flushed when a non-text event arrives.
        assistant_buf: list[str] = []

        def flush_assistant() -> None:
            if not assistant_buf:
                return
            text = "".join(assistant_buf).strip()
            assistant_buf.clear()
            if text:
                log.info("Assistant: %s", text)

        try:
            payload = build_session_start_payload(self._accept_call_id)

            with self._platform.keep_awake():
                await self._client.session_start(payload)
                log.info("Bridge session started (call_id=%s)", self._accept_call_id)

                # Nudge the talker to greet the user so they know the
                # assistant is connected and listening. Gemini Live only
                # speaks in response to input, so without this prompt the
                # caller hears silence until they speak first.
                try:
                    await self._client.send_text(_GREETING_PROMPT)
                except Exception:
                    log.exception("Failed to send greeting prompt")

                async for event in self._client.receive():
                    if event.type == RealtimeEventType.TEXT:
                        self._transcript.add_assistant_text(event.text)
                        assistant_buf.append(event.text)

                    elif event.type == RealtimeEventType.TOOL_CALL:
                        flush_assistant()
                        tc = event.tool_call
                        if tc is None:
                            continue
                        self._transcript.add_tool_call(tc)
                        log.info("Tool: %s(%s)", tc.name, tc.arguments)
                        if self._talker_tool_observer is not None:
                            try:
                                self._talker_tool_observer(tc.name, tc.arguments)
                            except Exception:
                                log.exception("talker_tool_observer raised")

                        result = await self._handle_tool_call(tc.name, tc.arguments)
                        self._transcript.add_tool_result(tc.id, result.message)
                        await self._client.send_tool_result(
                            tc.id,
                            tc.name,
                            result.message,
                            is_async=result.is_async,
                        )

                    elif event.type == RealtimeEventType.ERROR:
                        flush_assistant()
                        log.error("Realtime error: %s", event.error)

                    elif event.type == RealtimeEventType.SCREEN_STATE:
                        flush_assistant()
                        if event.screen_state is not None:
                            await self._handle_screen_state_change(event.screen_state)

                    elif event.type == RealtimeEventType.DISCONNECTED:
                        flush_assistant()
                        log.info("TeachSession.run: DISCONNECTED received, breaking loop")
                        break

                    if self._stop_requested:
                        flush_assistant()
                        log.info(
                            "TeachSession.run: stop_requested=%s, breaking loop",
                            self._stop_reason,
                        )
                        break

                flush_assistant()

        except Exception as e:
            log.exception("TeachSession error")
            return SessionResult(
                transcript=self._transcript,
                error=str(e),
                duration=self._transcript.duration,
            )
        finally:
            if self._executor_task and not self._executor_task.done():
                self._executor_task.cancel()
                try:
                    await self._executor_task
                except asyncio.CancelledError:
                    pass
            # Cancel any in-flight finalize so it doesn't keep running
            # (and triggering executor side-effects) after the call ends.
            if self._finalize_task and not self._finalize_task.done():
                self._finalize_task.cancel()
                try:
                    await self._finalize_task
                except asyncio.CancelledError:
                    pass
            await self._client.disconnect()

        # If not finalized during conversation, finalize now
        skill = next(reversed(self._skills_by_name.values())) if self._skills_by_name else None
        skill_path = None
        if skill is None and self._builder.step_count > 0:
            try:
                skill = await self._builder.finalize(self._skill_llm)
                skill_path = self._save_skill(skill)
                self._remember_finalized_skill(skill)
            except Exception:
                log.exception("Skill finalization failed")
        elif skill is not None:
            skill_path = self._saved_skill_paths_by_name.get(skill.name)

        return SessionResult(
            transcript=self._transcript,
            skill=skill,
            skill_path=skill_path,
            duration=self._transcript.duration,
            validation_passed=self._last_validation_passed,
            validation_duration=self._last_validation_duration,
        )

    async def _handle_tool_call(self, name: str, args: dict) -> ToolResult:
        """Route tool calls to registered handlers.

        Bridge-routed tools never reach here — they're filtered out at the
        bridge layer per the `handler` field on each declaration. If we get
        a tool name we don't know, return an error string (most likely a
        misconfigured ``tool_routing.local_to_bridge`` declaration).
        """
        handler = self._tool_handlers.get(name)
        if handler is None:
            return ToolResult(f"Unknown tool: {name}")
        return await handler(args)

    # ── Skill editing tools ──────────────────────────────

    async def _tool_observe_step(self, args: dict) -> ToolResult:
        # Bridge fans evidence frames out as BINARY WS messages; the client
        # ring buffer holds the most-recent ~60 frames. flush() returns
        # [(jpeg_bytes, ts)] — same shape SkillBuilder expects.
        frames = self._client.evidence_buffer.flush()
        intent = args.get("intent", "")
        action = args.get("action", "")
        idx = self._builder.add_observed_step(
            intent=intent,
            action=action,
            screenshots=frames,
        )
        return ToolResult(f"Step {idx + 1} recorded: {intent} | {action}")

    async def _tool_revise_step(self, args: dict) -> ToolResult:
        idx = int(args.get("step_index", -1))
        updates = {}
        for key in ("action", "intent", "feedback"):
            if key in args:
                updates[key] = args[key]
        self._builder.revise_step(idx, **updates)
        return ToolResult(f"Step {idx + 1} revised.")

    async def _tool_remove_step(self, args: dict) -> ToolResult:
        idx = int(args.get("step_index", -1))
        self._builder.remove_step(idx)
        return ToolResult(f"Step {idx + 1} removed.")

    async def _tool_finalize_skill(self, args: dict) -> ToolResult:
        if self._executor_task and not self._executor_task.done():
            return ToolResult("Executor is still running. Wait for it to finish first.")
        if self._finalize_task and not self._finalize_task.done():
            return ToolResult("Finalization already in progress.")
        if self._builder.step_count == 0:
            return ToolResult("No steps to finalize.")
        self._finalize_task = asyncio.create_task(self._run_finalize())
        return ToolResult("Generating skill... I'll let you know when it's ready.", is_async=True)

    # ── Execution tools ──────────────────────────────────

    async def _tool_execute_step(self, args: dict) -> ToolResult:
        if self._executor_task and not self._executor_task.done():
            return ToolResult("Executor is busy. Wait for it to finish first.")
        user_request = args.get("user_request", "")
        transcription = self._transcript.last_voice_rounds(_EXECUTE_STEP_TRANSCRIPTION_ROUNDS)
        self._executor_task = asyncio.create_task(
            self._run_executor(user_request, transcription),
        )
        return ToolResult("Executing... I'll report the result when done.", is_async=True)

    async def _tool_replay_skill(self, args: dict) -> ToolResult:
        if self._executor_task and not self._executor_task.done():
            return ToolResult("Executor is busy. Wait for it to finish first.")
        skill_name = str(args.get("name", "")).strip()
        if not skill_name:
            return ToolResult("replay_skill requires a finalized skill name.")
        replay_skill = self._resolve_replay_skill(skill_name or None)
        if replay_skill is None:
            return ToolResult(f"No finalized skill named '{skill_name}' to replay.")
        self._builder.load_steps(replay_skill.steps, source="skill")
        self._executor_task = asyncio.create_task(self._run_replay(replay_skill))
        return ToolResult(
            (
                "Started replaying the skill in a fresh executor session. "
                "This message does not mean the actions is done, I'll report "
                "back when it's done."
            ),
            is_async=True,
        )

    async def _tool_interrupt_executor(self, args: dict) -> ToolResult:
        # Interrupt only aborts the in-flight turn; the executor session is
        # preserved so the next execute_step continues in the same Claude
        # context (it sees the interrupt as part of the conversation).
        if self._executor_task and not self._executor_task.done():
            await self._executor.interrupt()
            self._executor_task.cancel()
            self._executor_task = None
            return ToolResult(
                "Execution interrupted; ready for your next instruction.",
            )
        return ToolResult("Nothing is executing.")

    async def _tool_check_executor(self, args: dict) -> ToolResult:
        running = self._executor_task and not self._executor_task.done()
        if not running:
            return ToolResult("Executor is idle.")
        if not self._executor_messages:
            return ToolResult("Executor is running, no output yet.")
        lines = ["Executor is running. Recent activity:"]
        now = time.monotonic()
        for ts, msg in self._executor_messages:
            ago = int(now - ts)
            lines.append(f"  [{ago}s ago] {msg[:80]}")
        return ToolResult("\n".join(lines))

    # ── Session tools ────────────────────────────────────

    async def _tool_set_task(self, args: dict) -> ToolResult:
        self._task = args.get("task", "")
        return ToolResult(f"Task set: {self._task}")

    # ── Internal helpers ─────────────────────────────────

    def _build_executor_context(self) -> ExecutorContext:
        """Build structured context for the executor's first invocation.

        Returns an ExecutorContext so a provider can render it for its own
        model (Claude Code today; ACP / built-in tomorrow). For string-based
        providers, ``str(context)`` reproduces the legacy flat layout.

        Note on the screen_share keys: the values are full self-explanatory
        sentences (not bare values) so the executor's static system prompt
        doesn't have to declare semantics for them. This keeps the prompt
        owner-controlled — runtime values flow through context only.
        """
        completed = [
            f"{i + 1}. [{s.source}] {s.intent} | action={s.action}"
            for i, s in enumerate(self._builder.steps)
        ]
        extra: dict[str, str] = {}
        share_line = self._share_context_line()
        if share_line:
            extra["user_visible_surface"] = share_line
        if self._active_share_display is not None:
            n = self._active_share_display
            extra["display_routing"] = (
                f"The user is watching display {n}. To keep your view aligned "
                f"with theirs, activate the target app and use the screenshot "
                f"returned by activate_app for click, drag, mouse_move, or scroll "
                f"coordinates. Keyboard and clipboard tools are "
                f"scoped by the focused element and need no display routing."
            )
        return ExecutorContext(task=self._task, completed_steps=completed, extra=extra)

    def _share_context_line(self) -> str:
        """One-line description of what the user is currently looking at.

        Empty when no screen is being shared, so the executor falls back to
        its own active-display detection.
        """
        if self._active_share_mode not in ("share", "observe"):
            return ""
        label = self._active_share_label or "unknown surface"
        direction = (
            "the user is sharing their screen so you can watch"
            if self._active_share_mode == "observe"
            else "you are sharing your screen so the user can watch"
        )
        line = f"{direction}; visible surface: {label}"
        if (
            self._active_share_mode == "share"
            and self._active_share_display is not None
        ):
            line += f" (display {self._active_share_display})"
        return line

    async def _handle_screen_state_change(self, update) -> None:  # noqa: ANN001 — ScreenStateUpdate, fwd-ref free
        """React to a bridge screen_state update.

        Updates the cached share target and, if an executor task is in flight,
        nudges the executor with a follow-up message so its next screenshot
        targets the surface the user is actually viewing.
        """
        prev_mode = self._active_share_mode
        prev_label = self._active_share_label
        prev_display = self._active_share_display
        self._active_share_mode = update.mode or "off"
        self._active_share_label = update.source_label or ""
        # Only carry through display_index for share mode; observe mode
        # reflects the remote peer's screen, not a local display index.
        if self._active_share_mode == "share":
            self._active_share_display = update.display_index
        else:
            self._active_share_display = None

        if (self._active_share_mode == prev_mode
                and self._active_share_label == prev_label
                and self._active_share_display == prev_display):
            return

        log.info(
            "screen_state change: mode=%s label=%r display=%s "
            "(was mode=%s label=%r display=%s)",
            self._active_share_mode, self._active_share_label,
            self._active_share_display,
            prev_mode, prev_label, prev_display,
        )

        if not (self._executor_task and not self._executor_task.done()):
            return  # nothing running — next start_task picks up the new context

        if self._active_share_mode in ("share", "observe"):
            label = self._active_share_label or "unknown surface"
            display_hint = (
                " Activate the target app and use the screenshot returned by "
                "activate_app for click, drag, mouse_move, or scroll "
                "coordinates so your view and actions match the user's display."
                if (
                    self._active_share_mode == "share"
                    and self._active_share_display is not None
                )
                else ""
            )
            notice = (
                f"[user-visible surface changed] The human is now viewing: "
                f"{label}.{display_hint} Take a fresh screenshot, describe "
                f"what you see, and ask the user to confirm the view matches "
                f"what they see. If it doesn't match, screenshot a different "
                f"display and describe again until you and the user agree on "
                f"the visible surface."
            )
        else:
            notice = (
                "[user-visible surface changed] Screen sharing has stopped; "
                "the user can no longer see your screen."
            )

        try:
            await self._executor.send_message(notice)
        except Exception:
            log.exception("Failed to notify executor of screen_state change")

    def _state_summary(self) -> str:
        """Build a minimal state summary for Talker memory."""
        executor = "running" if self._executor_task and not self._executor_task.done() else "idle"
        return f"task={self._task or 'not set'} executor={executor}"

    def _remember_finalized_skill(self, skill: Skill) -> None:
        self._skills_by_name.pop(skill.name, None)
        self._skills_by_name[skill.name] = skill

    def _save_skill(self, skill: Skill) -> Path:
        """Persist a finalized skill to the configured taught skills directory."""
        from protean.config import ProteanConfig
        from protean.skills.renderer import render_skill

        config = ProteanConfig.load()
        skill_dir = config.skills_dir / skill.name
        md_path = render_skill(skill, skill_dir)
        self._saved_skill_paths_by_name[skill.name] = md_path
        return md_path

    def _resolve_replay_skill(self, skill_name: str | None) -> Skill | None:
        if not skill_name:
            return None
        return self._skills_by_name.get(skill_name)

    def _resolve_replay_skill_dir(self, skill: Skill) -> Path:
        """Resolve the on-disk skill directory used for replay references."""
        saved_path = self._saved_skill_paths_by_name.get(skill.name)
        if saved_path is not None:
            return saved_path.parent

        from protean.config import ProteanConfig

        config = ProteanConfig.load()
        return config.skills_dir / skill.name

    async def _reset_executor_session(self) -> None:
        """Reset executor state so the next task starts a fresh session."""
        await self._executor.close()
        self._executor_started = False
        self._executor_messages.clear()

    async def _collect_executor_result(self) -> tuple[str, list[tuple[bytes, float]]]:
        """Consume executor events until completion and return result + key screenshots."""
        from protean.executor import ExecutorEventType as EET

        done_msg = ""
        all_messages = []
        finished_ts = 0.0
        screenshots: list[tuple[bytes, float]] = []

        start_ts = time.monotonic()
        async for evt in self._executor.get_events():
            if self._executor_event_observer is not None:
                try:
                    self._executor_event_observer(evt)
                except Exception:
                    log.exception("executor_event_observer raised")
            if evt.type == EET.TOOL_CALL:
                log.info("Executor tool: %s", evt.tool_name)
            elif evt.type == EET.TOOL_RESULT:
                ts = time.monotonic()
                overview_images = [img for img in evt.images if img[2] != "detail"]
                for data, _mime, _role in overview_images or evt.images[:1]:
                    screenshots.append((data, ts))
            elif evt.type == EET.MESSAGE:
                self._executor_messages.append((time.monotonic(), evt.message))
                self._executor_messages = self._executor_messages[-5:]
                all_messages.append(evt.message)
            elif evt.type == EET.DONE:
                done_msg = evt.message
                finished_ts = time.monotonic()
                log.info(
                    "Executor DONE after %.1fs (msg_len=%d, messages=%d)",
                    finished_ts - start_ts, len(done_msg), len(all_messages),
                )
                break
            elif evt.type == EET.ERROR:
                done_msg = evt.error or evt.message or "Unknown error"
                finished_ts = time.monotonic()
                log.warning(
                    "Executor ERROR after %.1fs: %s",
                    finished_ts - start_ts, done_msg[:200],
                )
                break

        result = done_msg or (all_messages[-1] if all_messages else "Done")

        # Skip the talker notification when this DONE was an interrupt —
        # the interrupt_executor tool already returned "Execution
        # interrupted; ready for your next instruction" to Gemini.
        # Double-notifying confuses Gemini (it acts as if a second event
        # happened) and can cascade into Gemini Live 1008 disconnects.
        if result.strip().lower() == "interrupted":
            log.info("Skipping DONE notification for interrupt (avoids double signal)")
            return result, screenshots

        notify_text = result[:300]
        log.info(
            "Sending DONE notification to talker (len=%d, screenshots=%d): %s",
            len(notify_text), len(screenshots), notify_text[:120].replace("\n", " | "),
        )
        try:
            await self._client.send_notification(notify_text)
            log.info("DONE notification delivered to bridge")
        except Exception:
            log.exception("Failed to deliver DONE notification to bridge")
        return result, screenshots

    async def _run_executor(
        self,
        user_request: str,
        transcription: list[tuple[str, str]] | None = None,
    ) -> None:
        """Execute a single step and record the result in the draft.

        The talker passes a single ``user_request`` (its verbatim quote of
        the user's latest instruction). Python additionally injects the
        recent voice ``transcription`` (last N rounds) as context so the
        executor can resolve referring expressions and corrections the
        talker may have summarized away. Only ``user_request`` is stored
        in the recorded skill step.
        """
        parts: list[str] = []
        if transcription:
            convo_lines = "\n".join(f"{role}: {content}" for role, content in transcription)
            parts.append(f"Recent conversation (context):\n{convo_lines}\n")
        parts.append(f"User request (authoritative, work until satisfied): {user_request}\n")
        parts.append(
            "Use your own screenshot tools to read the UI. Stop when the request is satisfied; report what you did."  # noqa: E501
        )
        framed = "\n".join(parts)

        try:
            if not self._executor_started:
                context = self._build_executor_context()
                await self._executor.start_task(framed, context=context)
                self._executor_started = True
            else:
                await self._executor.send_message(framed)

            result, screenshots = await self._collect_executor_result()
            self._builder.add_executed_step(
                intent=user_request,
                action=user_request,
                result=result,
                screenshots=screenshots,
            )
        except asyncio.CancelledError:
            log.info("Executor task cancelled")
        except Exception as e:
            log.exception("Executor error")
            await self._client.send_notification(f"Error: {e}")

    async def _run_replay(self, skill: Skill) -> None:
        """Replay a finalized skill by sending the full rendered skill to executor."""
        from protean.skills.renderer import render_skill_markdown

        try:
            await self._reset_executor_session()
            skill_md = render_skill_markdown(skill)
            instruction = "\n".join([
                f"Execute the skill '{skill.name}' end-to-end.",
                "Treat the rendered SKILL.md below as a reference workflow that "
                "captures one way to accomplish the goal via the GUI.",
                "Execute the whole workflow without waiting for step-by-step prompts.",
                "If a tool like `run_terminal_command` can directly accomplish the "
                "user's goal, PREFER that over reproducing the GUI steps — the "
                "SKILL.md is not binding when a faster path exists.",
                "If the current UI differs slightly, adapt naturally while preserving the goal.",
                "",
                "Rendered SKILL.md:",
                skill_md,
            ])
            await self._executor.start_task(instruction)
            self._executor_started = True
            await self._collect_executor_result()
        except asyncio.CancelledError:
            log.info("Replay cancelled for skill '%s'", skill.name)
        except Exception as e:
            await self._client.send_notification(f"Replay failed: {e}")

    async def _run_finalize(self) -> None:
        """Finalize skill in background. Notify Talker when done.

        Auto-validation was removed: it silently re-ran the skill on the
        user's desktop and the 30s+ runtime tended to trigger Gemini
        Live 1008 disconnects. Validation is now an explicit user-driven
        action (e.g. via replay_skill).
        """
        try:
            skill = await self._builder.finalize(self._skill_llm)
            skill_path = self._save_skill(skill)
            self._remember_finalized_skill(skill)
            self._builder = SkillBuilder()
            await self._client.send_notification(
                f"Skill '{skill.name}' saved to {skill_path} with {len(skill.steps)} steps."
            )
        except Exception as e:
            log.exception("Skill finalization failed")
            await self._client.send_notification(f"Finalization failed: {e}")

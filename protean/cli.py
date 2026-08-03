"""Protean CLI — record user behavior and generate skills.

Usage:
    protean daemon                    Start daemon with hotkey toggle
    protean record [--output DIR]     Manual record via Ctrl+C
    protean generate RECORDING_DIR    Generate skill from recording
    protean skills list               List available skills
    protean skills show SKILL_NAME    Show skill details
    protean skills run SKILL_NAME     Run a skill end-to-end
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click

from protean.config import DEFAULT_MODEL_ANTHROPIC, ProteanConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from protean.executor import ExecutorEvent, ExecutorProvider
    from protean.platform.base import Platform


def _build_executor(
    name: str,
    config: ProteanConfig,
    platform: "Platform",
    *,
    provider: str | None = None,
    model: str | None = None,
    working_dir: str | None = None,
) -> "ExecutorProvider":
    """Construct an ExecutorProvider by name, applying per-backend kwargs.

    Shared by ``daemon`` and ``skills run`` so a new -E choice only needs
    wiring in one place. ``claude_code`` runs Claude Code via the SDK and
    only needs ``platform`` (+ optional working_dir). ``computer_use`` runs
    Anthropic/OpenAI Computer Use and needs the provider's api_key/model.
    """
    from protean.executor import get_executor_provider

    if name == "claude_code":
        return get_executor_provider(
            "claude_code",
            platform=platform,
            working_dir=working_dir,
        )

    provider_name = provider or config.default_provider
    provider_cfg = config.llm_providers.get(provider_name, {})
    return get_executor_provider(
        "computer_use",
        api_key=provider_cfg.get("api_key", ""),
        model=model or provider_cfg.get("model", DEFAULT_MODEL_ANTHROPIC),
        base_url=provider_cfg.get("base_url"),
        platform=platform,
        image_keep_last=config.image_keep_last,
        enable_terminal=config.cua_enable_terminal,
        mcp_terminal_command=config.cua_terminal_command,
    )


def _sync_installed_agents(config: ProteanConfig) -> list[str]:
    """Re-propagate `config.skills_dir` to every installed agent runtime.

    Called after any pipeline that writes a skill (record/daemon-hotkey,
    generate, trajectories evolve, skills run --refine) so Codex / Claude
    Code see the new or updated skill without the user re-running
    ``protean agents setup``. Returns the list of target display names
    that were synced; empty list means no runtime was set up.
    """
    from protean.agent_setup import sync_installed_agents

    results = sync_installed_agents(config.skills_dir)
    return [r.target.display_name for r in results]


def _make_overlay_event_writer() -> "Callable[[ExecutorEvent], None]":
    """Build an overlay writer that mirrors `skills run --overlay` output.

    Caller must have already opened an overlay window (e.g. via
    `enable_overlay_for_display`). Each ExecutorEvent updates the overlay
    title (iteration / done) and appends a line for tool_call, tool_result
    (truncated to 200 chars), message, error, done.
    """
    from protean.executor.providers.computer_use import _MAX_ITERATIONS
    from protean.overlay import set_overlay_title, write_line

    def _write(evt: "ExecutorEvent") -> None:
        if evt.type.value == "iteration":
            title = f"Iter {evt.message}"
            if evt.input_tokens or evt.output_tokens:
                title += (
                    f" [{evt.input_tokens:.2E} in"
                    f" / {evt.output_tokens:.2E} out]"
                )
            set_overlay_title(title)
            ts = time.strftime("%H:%M:%S")
            write_line(f"{'━' * 40}")
            write_line(f"{ts} Iteration {evt.message} / {_MAX_ITERATIONS}")
            write_line(f"{'━' * 40}")
        elif evt.type.value == "tool_call":
            write_line(f"→ {evt.tool_name} {evt.tool_args}")
        elif evt.type.value == "tool_result":
            result = evt.result[:200] if evt.result else ""
            write_line(f"  ✓ {result}")
        elif evt.type.value == "message":
            if evt.message:
                write_line(f"  {evt.message}")
        elif evt.type.value == "error":
            write_line(f"✗ {evt.error}")
        elif evt.type.value == "done":
            set_overlay_title("Done")
            if evt.message:
                write_line(f"── Done: {evt.message}")

    return _write


def _make_overlay_talker_writer() -> "Callable[[str, dict], None]":
    """Build an overlay writer for talker (realtime LLM) tool calls.

    Caller must have already opened an overlay window. Each call appends
    a single line so the user can see what the talker is requesting in
    real time (execute_step, observe_step, set_task, finalize_skill, etc.).
    """
    from protean.overlay import write_line

    def _write(name: str, args: dict) -> None:
        write_line(f"⟹ talker: {name}({args})")

    return _write


@click.group()
@click.pass_context
def main(ctx: click.Context) -> None:
    """Protean — record once, skill forever."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",    )
    # Subprocesses we drive (most notably the Claude Code CLI via
    # claude-agent-sdk) can leave the parent terminal in raw mode
    # (-onlcr), where bare \n stops auto-translating to \r\n. Log lines
    # then stair-step rightward instead of returning to column 0. Force
    # CRLF on TTY stream handlers — harmless on cooked terminals,
    # restores readability on raw ones. Only applies to interactive
    # terminals so piping to files / journals stays clean.
    if sys.stderr.isatty():
        for _h in logging.getLogger().handlers:
            if isinstance(_h, logging.StreamHandler):
                _h.terminator = "\r\n"
    ctx.ensure_object(dict)
    ctx.obj["config"] = ProteanConfig.load()


@main.command()
@click.option("--output", "-o", type=click.Path(), default=None, help="Output directory")
@click.option(
    "--display",
    "-d",
    type=int,
    default=None,
    help="Display index (1-based, 1=primary). Omit to auto-detect or list.",
)
@click.option("--no-click-markers", is_flag=True, help="Don't show click markers in video")
@click.option(
    "--audio/--no-audio",
    default=True,
    help="Record microphone with VAD + ASR and emit SPEECH events (default: on)",
)
@click.option("--no-screenshots", is_flag=True, help="Disable per-event screenshot capture")
@click.pass_context
def record(
    ctx: click.Context, output: str | None, display: int | None, no_click_markers: bool,
    audio: bool, no_screenshots: bool,
) -> None:
    """Record a user demonstration (screen + keyboard + mouse).

    Dual-track recording:
      Track 1: Screen video (.mov) — REQUIRED (grant screen recording permission)
      Track 2: Input events (keyboard/mouse with window context) → events.json

    All output files are dumped to a single directory. The exact paths
    are printed at start so you always know where everything goes.
    """
    config: ProteanConfig = ctx.obj["config"]

    from protean.platform import get_platform
    from protean.recorder.session import RecordingConfig, RecordingSession, detect_active_display

    platform = get_platform()

    # Auto-detect which display the cursor is on
    if display is None:
        display = detect_active_display(platform)

    # ── Output directory ──
    if output:
        output_dir = Path(output)
    else:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_dir = config.recordings_dir / f"rec-{timestamp}"

    rec_config = RecordingConfig(
        output_dir=output_dir,
        display_index=display,
        show_clicks=not no_click_markers,
        record_audio=audio,
        screenshots=not no_screenshots,
    )

    session = RecordingSession(rec_config, platform)

    # ── Start recording — print all paths ──
    try:
        start_info = session.start()
    except RuntimeError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)

    click.echo("=" * 60)
    click.echo("  RECORDING STARTED")
    click.echo("=" * 60)
    click.echo()
    click.echo("  Output files:")
    click.echo(f"    Directory:  {start_info['output_dir']}")
    if start_info.get("video_file"):
        click.echo(f"    Video:      {start_info['video_file']}")
    if start_info.get("screenshots_dir"):
        click.echo(f"    Screenshots:{start_info['screenshots_dir']}")
    if start_info.get("audio_dir"):
        click.echo(f"    Audio:      {start_info['audio_dir']}")
    click.echo(f"    Events:     {start_info['events_file']}")
    click.echo("    Mode:       video + screenshots")
    click.echo()
    di = start_info.get("display_info")
    if di:
        primary_tag = " (primary)" if di["primary"] else ""
        click.echo(
            f"  Recording display {start_info['display_index']}: {di['resolution']}{primary_tag}"
        )
    else:
        click.echo(f"  Recording display {start_info['display_index']}")
    click.echo()
    click.echo("  Perform your task now. Press Ctrl+C to stop.")
    click.echo("=" * 60)
    click.echo()

    # Handle Ctrl+C gracefully
    def on_interrupt(sig: int, frame: object) -> None:
        click.echo("\n\nStopping recording...")
        result = session.stop()

        click.echo()
        click.echo("=" * 60)
        click.echo("  RECORDING COMPLETE")
        click.echo("=" * 60)
        click.echo()
        click.echo(f"  Duration:      {result.duration:.1f}s")
        click.echo(f"  Total events:  {result.event_count}")
        click.echo(f"  Display:       {result.display_index}")
        click.echo()
        click.echo("  Output files:")
        if result.video_file:
            click.echo(f"    Video:       {result.video_file}")
        click.echo(f"    Events:      {result.events_file}")
        click.echo()
        click.echo("  Event breakdown:")
        for et, count in sorted(result.summary.items(), key=lambda x: -x[1]):
            click.echo(f"    {et}: {count}")
        click.echo()
        click.echo("  Next step:")
        click.echo(f"    protean generate {output_dir}")
        click.echo()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_interrupt)

    # Block until interrupted
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


@main.command()
@click.option(
    "--hotkey",
    default="alt+shift+r",
    help="Global hotkey to toggle recording (default: Option+Shift+R)",
)
@click.option("--provider", "-p", default=None, help="LLM provider for skill generation")
@click.option("--model", "-m", default=None, help="Model name override")
@click.option(
    "-E", "--executor", "executor_name",
    type=click.Choice(["computer_use", "claude_code"]),
    default="computer_use",
    show_default=True,
    help="CUA backend used by realtime TeachSession and auto-validation",
)
@click.option(
    "--overlay/--no-overlay", default=False, show_default=True,
    help="Show capture-proof overlay window with executor events",
)
@click.pass_context
def daemon(
    ctx: click.Context,
    hotkey: str,
    provider: str | None,
    model: str | None,
    executor_name: str,
    overlay: bool,
) -> None:
    """Run Protean as a background daemon with global hotkey toggles.

    hotkey (Option+Shift+R): toggle screen recording (no audio).

    Subscribes to the Electron bridge for room state. When a remote user
    joins the configured TRTC room (via presence service), the bridge emits
    ``room_state(state="ringing")`` and Protean auto-starts a TeachSession.

    The bridge owns TRTC + realtime LLM (Gemini) + presence. Set
    ``PROTEAN_BRIDGE_REALTIME=gemini`` plus ``GEMINI_API_KEY`` in the bridge
    env to talk to a real Gemini Live model; otherwise the bridge falls back
    to ``transport.local_mock`` for development.
    """
    config: ProteanConfig = ctx.obj["config"]

    import threading

    from protean.channels.bridge_supervisor import BridgeError, BridgeSupervisor
    from protean.channels.realtime_bridge import RealtimeBridgeClient
    from protean.platform import get_platform
    from protean.recorder.session import RecordingConfig, RecordingSession, detect_active_display

    platform = get_platform()
    supervisor = BridgeSupervisor()

    # Backup recording state
    session: RecordingSession | None = None
    is_call_recording = False
    teach_session_thread: threading.Thread | None = None
    teach_session_stop: threading.Event = threading.Event()

    def _stop_and_generate() -> None:
        nonlocal session, is_call_recording
        platform.notify("Protean", "Stopping recording...")
        assert session is not None
        result = session.stop()
        session = None
        is_call_recording = False

        # Manual recordings: Spotlight-style prompt for task description
        description = platform.prompt_text(
            "Protean — Describe what you just demonstrated",
            placeholder="e.g., Open Settings and change the display name",
        )
        if description is None:
            platform.notify("Protean", "Recording saved (no description). Generating skill...")
            description = ""
        else:
            platform.notify(
                "Protean",
                f"Recorded {result.duration:.0f}s, "
                f"{result.event_count} events.\nGenerating skill...",
            )

        import traceback

        from protean.llm import create_llm_from_config

        async def _generate() -> None:
            from protean.skills.builder import SkillBuilder

            try:
                llm = create_llm_from_config(
                    config.llm_providers, config.default_provider, provider
                )
                skill, md_path = await SkillBuilder.from_recording_and_save(
                    llm,
                    result.output_dir,
                    config.skills_dir,
                    task_description=description,
                    model=model,
                    max_tokens=config.skill_max_tokens,
                    temperature=config.skill_temperature,
                )
                click.echo(f"Skill '{skill.name}' saved to: {md_path.parent}")
                platform.notify(
                    "Protean — Skill Ready",
                    f"Skill '{skill.name}' generated!\n{md_path}",
                )

                synced = _sync_installed_agents(config)
                if synced:
                    click.echo(f"Synced to: {', '.join(synced)}")

                # Auto-validate the generated skill
                try:
                    from protean.skills.runner import RunMode, StepRunner

                    executor = _build_executor(
                        executor_name,
                        config,
                        platform,
                        provider=provider,
                        model=model,
                    )
                    try:
                        runner = StepRunner(executor, platform, llm)
                        report = await runner.run(
                            skill, mode=RunMode.VALIDATE, skill_dir=md_path.parent,
                        )
                        status = "PASSED" if report.passed else "FAILED"
                        platform.notify(
                            f"Protean — Validation {status}",
                            f"Skill '{skill.name}' validation: {status} ({report.duration:.1f}s)",
                        )
                    finally:
                        await executor.close()
                except Exception as e:
                    platform.notify("Protean — Validation Error", str(e))

            except Exception as e:
                err_msg = f"Skill generation failed: {e}"
                platform.notify("Protean — Error", err_msg)
                # Write error to log file next to the recording
                log_path = result.output_dir / "error.log"
                log_path.write_text(f"{err_msg}\n\n{traceback.format_exc()}", encoding="utf-8")

        asyncio.run(_generate())

    def _start_recording(capture_audio: bool = False, display_index: int | None = None) -> None:
        nonlocal session
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_dir = config.recordings_dir / f"rec-{timestamp}"
        rec_config = RecordingConfig(
            output_dir=output_dir,
            display_index=display_index or detect_active_display(platform),
            capture_audio=capture_audio,
            # In TRTC mode the bridge owns the mic and forwards PCM to the
            # realtime LLM directly; the legacy mic+VAD+ASR worker would
            # both duplicate the capture and hammer a remote ASR endpoint
            # that's often unreachable. Tie record_audio to capture_audio
            # so disabling system-audio capture also disables mic capture.
            record_audio=capture_audio,
        )
        session = RecordingSession(rec_config, platform)
        session.start()

    def on_record_hotkey() -> None:
        nonlocal session, is_call_recording
        try:
            if session is None:
                _start_recording(capture_audio=False)
                is_call_recording = False
                platform.notify(
                    "Protean — Recording",
                    "Recording started (no audio).\nPress hotkey again to stop.",
                )
            else:
                _stop_and_generate()
        except Exception as e:
            platform.notify("Protean — Error", str(e))
            session = None
            is_call_recording = False

    # Register hotkey
    record_keys = [k.strip() for k in hotkey.split("+")]
    unreg1 = platform.register_hotkey(record_keys, on_record_hotkey)

    # Spawn bridge once for the lifetime of the daemon.
    try:
        bootstrap = asyncio.run(supervisor.start())
    except BridgeError as e:
        click.echo(f"Failed to start bridge: {e}", err=True)
        unreg1()
        sys.exit(1)

    click.echo("Protean daemon running.")
    click.echo(f"  Record hotkey: {hotkey}")
    click.echo(f"  Bridge: pid={bootstrap.pid} port={bootstrap.port}")
    if supervisor.stderr_log_path is not None:
        click.echo(f"  Bridge log: {supervisor.stderr_log_path}")
        click.echo("    (tail -f to watch heartbeats / errors live)")
    click.echo("Ctrl+C to quit.")
    platform.notify(
        "Protean",
        f"Daemon started.\n{hotkey} = record\nBridge ready (pid={bootstrap.pid}).",
    )

    # Bridge event subscription runs in a background thread with its own
    # asyncio loop. The main thread stays free for the hotkey listener
    # (pynput requires the main thread on macOS for TIS/TSM access).
    bridge_loop_ready = threading.Event()
    bridge_loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    def _run_bridge_loop() -> None:
        loop = asyncio.new_event_loop()
        bridge_loop_holder["loop"] = loop
        asyncio.set_event_loop(loop)
        bridge_loop_ready.set()
        try:
            loop.run_until_complete(_bridge_event_loop())
        finally:
            loop.close()

    async def _bridge_event_loop() -> None:
        """Subscribe to bridge events, kick off TeachSession on ringing."""
        nonlocal session, is_call_recording, teach_session_thread

        # Outer loop: each iteration drives one bridge listener client. After
        # a TeachSession ends we open a fresh client and re-iterate, because
        # the previous client.events() iterator is bound to the closed WS
        # and won't yield further events.
        log = logging.getLogger("protean.daemon")
        while True:
            client = RealtimeBridgeClient(bootstrap.port, bootstrap.token)
            await client.connect(daemon_version="0.1.0")
            log.info("Bridge listener connected (waiting for ringing)")

            async for evt in client.events():
                if evt.type != "room_state":
                    continue
                state = evt.payload.get("state")
                call_id = str(evt.payload.get("call_id", ""))
                log.info("room_state %s call_id=%s", state, call_id)

                if state == "ringing" and session is None:
                    # Close our listener client so TeachSession can take the slot.
                    await client.close()

                    # TRTC owns audio in this path — the bridge captures the
                    # caller's mic via TRTC and forwards to the realtime LLM.
                    # The legacy local-mic recorder would (a) duplicate the
                    # capture and (b) hammer a remote ASR endpoint that's
                    # often unreachable from the dev box (WinError 10060).
                    # Keep recording entirely off in TRTC mode.
                    _start_recording(capture_audio=False)
                    is_call_recording = False
                    teach_session_stop.clear()

                    def _run_teach() -> None:
                        try:
                            _start_teach_session(
                                config, platform, bootstrap.port, bootstrap.token,
                                call_id, provider, model, executor_name,
                                teach_session_stop, overlay,
                            )
                        except Exception as e:
                            platform.notify("Protean — Error", f"TeachSession failed: {e}")

                    teach_session_thread = threading.Thread(target=_run_teach, daemon=True)
                    teach_session_thread.start()
                    platform.notify(
                        "Protean — Call Active",
                        f"Joined room {call_id}. Recording + realtime session started.",
                    )
                    # Wait for TeachSession to finish, then break out of the
                    # inner async-for so the outer while-loop reopens a fresh
                    # listener for the next call.
                    await asyncio.get_running_loop().run_in_executor(
                        None, teach_session_thread.join,
                    )
                    log.info("TeachSession thread joined; reopening listener")
                    if session is not None and is_call_recording:
                        try:
                            session.stop()
                        except Exception:
                            log.exception("Backup recording stop failed")
                        session = None
                        is_call_recording = False
                    break

                elif state == "ended":
                    # Always trip the stop event so TeachSession winds down on
                    # remote hangup. Backup-recording teardown is conditional;
                    # session shutdown is not.
                    teach_session_stop.set()
                    if session is not None and is_call_recording:
                        platform.notify("Protean", "Call ended. Stopping recording...")

    bridge_thread = threading.Thread(target=_run_bridge_loop, daemon=True)
    bridge_thread.start()
    bridge_loop_ready.wait(timeout=5)

    # Main thread polls only for KeyboardInterrupt — the bridge thread
    # drives session lifecycle.
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        unreg1()
        teach_session_stop.set()
        try:
            asyncio.run(supervisor.stop())
        except Exception:
            pass
        click.echo("\nDaemon stopped.")


def _start_teach_session(
    config: ProteanConfig,
    platform: Platform,
    bridge_port: int,
    bridge_token: str,
    accept_call_id: str,
    provider: str | None,
    model: str | None,
    executor_name: str,
    stop_event: "threading.Event",
    overlay: bool = False,
) -> None:
    """Run a TeachSession in this thread with its own asyncio loop."""
    from protean.channels.realtime_bridge import RealtimeBridgeClient
    from protean.channels.realtime_llm_bridge import BridgeBackedRealtimeLLM
    from protean.llm import create_llm_from_config
    from protean.realtime.session import TeachSession

    executor = _build_executor(
        executor_name, config, platform, provider=provider, model=model,
    )
    skill_llm = create_llm_from_config(
        config.llm_providers, config.default_provider, provider,
    )
    # `model` is reserved for future use — TeachSession picks per-purpose
    # models internally based on config.
    _ = model

    overlay_observer: "Callable[[ExecutorEvent], None] | None" = None
    overlay_talker_observer: "Callable[[str, dict], None] | None" = None
    overlay_cm: "contextlib.AbstractContextManager[Any]" = contextlib.nullcontext()
    if overlay:
        from protean.overlay import enable_overlay_for_display
        from protean.platform.base import active_display
        maybe_display = active_display(platform)
        overlay_display = maybe_display.display_index if maybe_display is not None else 1
        overlay_cm = enable_overlay_for_display(display=overlay_display)
        overlay_observer = _make_overlay_event_writer()
        overlay_talker_observer = _make_overlay_talker_writer()

    async def _run() -> None:
        bridge_client = RealtimeBridgeClient(bridge_port, bridge_token)
        await bridge_client.connect(daemon_version="0.1.0")
        adapter = BridgeBackedRealtimeLLM(bridge_client)

        teach = TeachSession(
            realtime_client=adapter,
            platform=platform,
            skill_llm=skill_llm,
            executor=executor,
            accept_call_id=accept_call_id,
            executor_event_observer=overlay_observer,
            talker_tool_observer=overlay_talker_observer,
        )

        # Watcher: when the bridge thread sets stop_event (e.g. user
        # hung up via room_state.ended), ask TeachSession to wind down.
        # request_stop() alone won't unblock TeachSession's `async for ...
        # receive()` because the realtime stream may be silent after the
        # remote leaves — disconnecting the adapter pushes a DISCONNECTED
        # event onto its queue, which causes the loop to break promptly.
        async def _watch_stop() -> None:
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
            teach.request_stop("call-ended")
            try:
                await adapter.disconnect()
            except Exception:
                logging.getLogger("protean.daemon").exception(
                    "Watcher disconnect failed"
                )

        watcher = asyncio.create_task(_watch_stop())
        try:
            result = await teach.run()
        finally:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
            await bridge_client.close()

        if result.skill and result.skill_path:
            platform.notify(
                "Protean — Skill Ready",
                f"Skill '{result.skill.name}' saved!\n{result.skill_path}",
            )
        elif result.skill:
            platform.notify(
                "Protean — Skill Ready",
                f"Skill '{result.skill.name}' generated.",
            )
        elif result.error:
            platform.notify("Protean — Error", result.error)
        else:
            platform.notify(
                "Protean", "Call ended. No skill generated.",
            )

    with overlay_cm:
        asyncio.run(_run())


@main.command()
@click.argument("recording_dir", type=click.Path(exists=True))
@click.option(
    "--provider", "-p", default=None, help="LLM provider (openai/anthropic/gemini/doubao)"
)
@click.option("--model", "-m", default=None, help="Model name override")
@click.option("--description", "-d", default="", help="Describe what the recorded task does")
@click.option(
    "--no-images", is_flag=True, help="Don't send frame images to LLM (text-only analysis)"
)
@click.option("--output", "-o", type=click.Path(), default=None, help="Output skill directory")
@click.option("--validate", is_flag=True, default=False, help="Run validation after generation")
@click.option(
    "--image-budget",
    type=int,
    default=None,
    help="Max images attached to the LLM prompt (escape hatch — bypasses "
    "MAX_CONTEXT-derived budget AND the 100-image hard cap; "
    "overrides PROTEAN_GENERATE_IMAGE_BUDGET env var)",
)
@click.option(
    "--max-context",
    type=int,
    default=None,
    help="Target model context window in tokens (200000 / 400000 / 1000000); "
    "image budget is derived from this. Set PROTEAN_MAX_CONTEXT in .env "
    "for the default; this flag overrides it.",
)
@click.pass_context
def generate(
    ctx: click.Context,
    recording_dir: str,
    provider: str | None,
    model: str | None,
    description: str,
    no_images: bool,
    output: str | None,
    validate: bool,
    image_budget: int | None,
    max_context: int | None,
) -> None:
    """Generate a skill from a recording.

    Analyzes the recording using:
      1. Event compaction (merge typing/scrolling into semantic units)
      2. Dual-image frame extraction (overview + detail crop per action)
      3. LLM analysis with interleaved event+image pairs

    With --validate, automatically runs the generated skill through
    the validation pipeline to verify it works.
    """
    config: ProteanConfig = ctx.obj["config"]

    from protean.llm import create_llm_from_config
    from protean.skills.builder import SkillBuilder

    if image_budget is not None:
        os.environ["PROTEAN_GENERATE_IMAGE_BUDGET"] = str(image_budget)
    if max_context is not None:
        os.environ["PROTEAN_MAX_CONTEXT"] = str(max_context)

    llm = create_llm_from_config(config.llm_providers, config.default_provider, provider)

    click.echo(f"Analyzing recording: {recording_dir}")
    click.echo(f"Using LLM provider: {llm.provider}")
    if no_images:
        click.echo("(text-only mode — no frame images sent to LLM)")
    else:
        if max_context is not None:
            click.echo(f"(max context: {max_context} tok)")
        if image_budget is not None:
            click.echo(f"(image budget override: {image_budget})")

    async def _run() -> None:
        skill_dir = Path(output) if output else config.skills_dir
        skill, md_path = await SkillBuilder.from_recording_and_save(
            llm,
            Path(recording_dir),
            skill_dir,
            task_description=description,
            model=model,
            include_images=not no_images,
            max_tokens=config.skill_max_tokens,
            temperature=config.skill_temperature,
        )

        click.echo(f"\nSkill generated: {skill.name}")
        click.echo(f"  Description: {skill.description}")
        click.echo(f"  Goal:        {skill.goal}")
        click.echo(f"  Steps:       {len(skill.steps)}")
        click.echo(f"  Scripts:     {len(skill.scripts)}")
        click.echo(f"  Parameters:  {len(skill.parameters)}")
        click.echo(f"  Saved to:    {md_path.parent}")
        click.echo(f"\n--- {md_path.name} ---\n")
        click.echo(md_path.read_text(encoding="utf-8"))

        synced = _sync_installed_agents(config)
        if synced:
            click.echo(f"\nSynced to: {', '.join(synced)}")

        # ── Optional validation ────────────────────────────
        if validate:
            click.echo("\n--- Validating generated skill ---\n")
            from protean.executor import get_executor_provider
            from protean.platform import get_platform
            from protean.skills.runner import RunMode, StepRunner

            platform = get_platform()
            cua_provider = provider or config.default_provider
            cua_cfg = config.llm_providers.get(cua_provider, {})
            executor = get_executor_provider(
                "computer_use",
                api_key=cua_cfg.get("api_key", ""),
                model=cua_cfg.get("model", DEFAULT_MODEL_ANTHROPIC),
                base_url=cua_cfg.get("base_url"),
                platform=platform,
                image_keep_last=config.image_keep_last,
                enable_terminal=config.cua_enable_terminal,
                mcp_terminal_command=config.cua_terminal_command,
            )
            runner = StepRunner(executor, platform, llm)
            try:
                report = await runner.run(
                    skill,
                    mode=RunMode.VALIDATE,
                    skill_dir=md_path.parent,
                )
                status = "PASSED" if report.passed else "FAILED"
                click.echo(f"Validation: {status} ({report.duration:.1f}s)")
                for sv in report.steps:
                    icon = "+" if sv.result.value == "passed" else "x"
                    click.echo(f"  [{icon}] Step {sv.index + 1}: {sv.result.value} — {sv.reason}")
            except Exception as e:
                click.echo(f"Validation failed: {e}", err=True)
            finally:
                await executor.close()

    asyncio.run(_run())


@main.group()
def trajectories() -> None:
    """Inspect and evolve agent runtime trajectories."""
    pass


def _marker_store(config: ProteanConfig):
    from protean.trajectories.markers import TrajectoryMarkerStore

    return TrajectoryMarkerStore(config.data_dir / "trajectory_markers.jsonl")


def _resolve_cli_trajectory_slice(config: ProteanConfig, **kwargs):
    from protean.trajectories.resolver import resolve_trajectory_slice

    try:
        return resolve_trajectory_slice(
            marker_store_path=_marker_store(config).path,
            **kwargs,
        )
    except (FileNotFoundError, LookupError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@trajectories.command("mark")
@click.argument("phase", type=click.Choice(["start", "end"]))
@click.option(
    "--source",
    type=click.Choice(["codex", "claude_code"]),
    default="codex",
    show_default=True,
)
@click.option("--label", required=True, help="Short episode label")
@click.option("--task", default="", help="Task text for start markers")
@click.option("--session", default="current", show_default=True)
@click.pass_context
def trajectories_mark(
    ctx: click.Context,
    phase: str,
    source: str,
    label: str,
    task: str,
    session: str,
) -> None:
    """Record an explicit trajectory episode marker."""
    config: ProteanConfig = ctx.obj["config"]
    from protean.trajectories.markers import TrajectoryMarker, utc_now_iso
    from protean.trajectories.resolver import trajectory_adapter_cls

    try:
        marker_session = str(trajectory_adapter_cls(source).resolve_session(session))
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e

    marker = TrajectoryMarker(
        source=source,
        label=label,
        phase=phase,
        timestamp=utc_now_iso(),
        cwd=str(Path.cwd()),
        task=task,
        session=marker_session,
    )
    store = _marker_store(config)
    store.append(marker)
    click.echo(f"Recorded {phase} marker: source={source} label={label} session={marker_session}")
    click.echo(f"  {store.path}")
    if phase == "end":
        click.echo("")
        click.echo("You can inspect and evolve the skill now:")
        click.echo(
            f"  protean trajectories inspect --source {source} "
            f"--label {label} --session {marker_session}"
        )
        click.echo(
            f"  protean trajectories evolve  --source {source} "
            f"--label {label} --session {marker_session} --task \"<original task>\""
        )
        click.echo("  (run `evolve` detached so the chat is not blocked)")


@trajectories.command("inspect")
@click.option(
    "--source",
    type=click.Choice(["codex", "claude_code"]),
    default="codex",
    show_default=True,
)
@click.option("--session", default="current", show_default=True)
@click.option("--label", default="", help="Use latest matching marker pair")
@click.option("--from-message", default="", help="Begin at first message containing text")
@click.option("--to-message", default="", help="End at first later message containing text")
@click.option("--from-time", default="", help="Begin timestamp")
@click.option("--to-time", default="", help="End timestamp")
@click.pass_context
def trajectories_inspect(
    ctx: click.Context,
    source: str,
    session: str,
    label: str,
    from_message: str,
    to_message: str,
    from_time: str,
    to_time: str,
) -> None:
    """Summarize the selected current-session trajectory slice."""
    config: ProteanConfig = ctx.obj["config"]
    slice_ = _resolve_cli_trajectory_slice(
        config,
        source=source,
        session=session,
        label=label,
        from_message=from_message,
        to_message=to_message,
        from_time=from_time,
        to_time=to_time,
    )
    click.echo(f"Session: {slice_.session_path}")
    click.echo(f"Range:   {slice_.start_time.isoformat()} -> {slice_.end_time.isoformat()}")
    click.echo(
        f"Events:  raw={len(slice_.events)} "
        f"react={len(slice_.react_events)} tools={slice_.tool_count}"
    )
    used = slice_.used_skills
    click.echo(f"Skills used: {', '.join(used) if used else '(none detected)'}")
    click.echo("User messages:")
    for i, msg in enumerate(slice_.user_messages[:8], 1):
        click.echo(f"  {i}. {msg[:240]}")
    if len(slice_.user_messages) > 8:
        click.echo(f"  ... {len(slice_.user_messages) - 8} more")


@trajectories.command("evolve")
@click.option(
    "--source",
    type=click.Choice(["codex", "claude_code"]),
    default="codex",
    show_default=True,
)
@click.option("--session", default="current", show_default=True)
@click.option("--label", default="", help="Use latest matching marker pair")
@click.option("--from-message", default="", help="Begin at first message containing text")
@click.option("--to-message", default="", help="End at first later message containing text")
@click.option("--from-time", default="", help="Begin timestamp")
@click.option("--to-time", default="", help="End timestamp")
@click.option("--task", default="", help="Task context for evolution")
@click.option("--provider", default=None, help="LLM provider")
@click.option("--model", default=None, help="Model override")
@click.pass_context
def trajectories_evolve(
    ctx: click.Context,
    source: str,
    session: str,
    label: str,
    from_message: str,
    to_message: str,
    from_time: str,
    to_time: str,
    task: str,
    provider: str | None,
    model: str | None,
) -> None:
    """Evolve Protean skills from the selected trajectory slice."""
    config: ProteanConfig = ctx.obj["config"]
    slice_ = _resolve_cli_trajectory_slice(
        config,
        source=source,
        session=session,
        label=label,
        from_message=from_message,
        to_message=to_message,
        from_time=from_time,
        to_time=to_time,
    )

    async def _run() -> None:
        import json

        from protean.llm import create_llm_from_config
        from protean.skills.evolve import SkillEvolver

        llm = create_llm_from_config(config.llm_providers, config.default_provider, provider)
        task_name = task or label or f"{source}-session"
        trajectory = slice_.to_run_trajectory(
            task_name=task_name,
            verify_reason=f"Imported from {source} session slice: {slice_.session_path}",
        )
        evolver = SkillEvolver(
            config.skills_dir,
            llm,
            model=model,
            temperature=config.skill_temperature,
        )
        result = await evolver.evolve(
            trajectory,
            task_name=task_name,
            task_context=task or trajectory.task,
            used_skills=slice_.used_skills,
        )
        if result.skills_created or result.skills_refined or result.skills_deleted:
            synced = _sync_installed_agents(config)
            if synced:
                click.echo(f"Synced to: {', '.join(synced)}", err=True)
        click.echo(json.dumps({
            "actions_taken": result.actions_taken,
            "skills_created": result.skills_created,
            "skills_refined": result.skills_refined,
            "skills_deleted": result.skills_deleted,
        }, ensure_ascii=False, indent=2))

    asyncio.run(_run())


@main.group()
def agents() -> None:
    """Set up external agent runtimes to use Protean."""
    pass


@agents.command("setup")
@click.argument("target", type=click.Choice(["codex", "claude", "claude_code"]))
@click.pass_context
def agents_setup(
    ctx: click.Context,
    target: str,
) -> None:
    """Install Protean skills into Codex or Claude Code."""
    config: ProteanConfig = ctx.obj["config"]
    from protean.agent_setup import load_exportable_skill_pairs, resolve_agent_target, setup_agent

    try:
        agent_target = resolve_agent_target(target)
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    extra_pairs = load_exportable_skill_pairs(config.skills_dir)

    result = setup_agent(
        agent_target,
        extra_skill_pairs=extra_pairs,
    )
    click.echo(f"Target: {result.target.display_name}")
    click.echo(f"Skills dir: {result.bootstrap_path.parent.parent}")
    click.echo(f"Agent skill: {result.bootstrap_path}")
    if result.instructions_path is not None:
        click.echo(f"Instructions: {result.instructions_path}")
    if result.mcp_config_path is not None:
        click.echo(f"MCP config: {result.mcp_config_path}")
    if result.copied_skills:
        click.echo("Copied Protean skills:")
        for path in result.copied_skills:
            click.echo(f"  {path.name}: {path}")
    else:
        click.echo("No additional Protean skills copied.")


@agents.command("uninstall")
@click.argument(
    "target",
    type=click.Choice(["codex", "claude", "claude_code", "all"]),
)
@click.pass_context
def agents_uninstall(
    ctx: click.Context,
    target: str,
) -> None:
    """Remove Protean skills and managed instructions block from a runtime.

    Pass ``all`` to uninstall from every runtime that has Protean installed.
    """
    config: ProteanConfig = ctx.obj["config"]
    from protean.agent_setup import (
        installed_agent_targets,
        managed_skill_names,
        resolve_agent_target,
        uninstall_agent,
    )

    if target == "all":
        targets = installed_agent_targets()
        if not targets:
            click.echo("No installed Protean agent runtimes found.")
            return
    else:
        try:
            targets = [resolve_agent_target(target)]
        except ValueError as e:
            raise click.ClickException(str(e)) from e

    skill_names = managed_skill_names(config.skills_dir)

    for agent_target in targets:
        result = uninstall_agent(agent_target, managed_skill_names=skill_names)
        click.echo(f"Target: {result.target.display_name}")
        if result.removed_bootstrap is not None:
            click.echo(f"  Removed bootstrap skill: {result.removed_bootstrap}")
        else:
            click.echo("  Bootstrap skill: not installed")
        if result.removed_skills:
            click.echo("  Removed Protean skills:")
            for path in result.removed_skills:
                click.echo(f"    {path.name}: {path}")
        else:
            click.echo("  No additional Protean skills to remove.")
        if result.instructions_path is not None:
            verb = "Deleted" if result.instructions_file_deleted else "Cleaned"
            click.echo(f"  {verb} instructions file: {result.instructions_path}")
        else:
            click.echo("  Instructions: no managed block to remove")
        if result.mcp_config_path is not None:
            click.echo(f"  Cleaned MCP config: {result.mcp_config_path}")
        else:
            click.echo("  MCP config: no managed entry to remove")


@main.command("mcp")
@click.pass_context
def mcp(ctx: click.Context) -> None:
    """Serve Protean's GUI tools as an MCP server over stdio.

    Designed to be wired into external CLI agents (Codex, Claude Code,
    ...) via their ``mcp_servers`` config. The server exposes Platform
    methods — screenshot, click, mouse_move, drag, type_text, key_press,
    scroll, wait, list_windows, activate_window, activate_app,
    get_active_window, get_clipboard — so the agent can
    drive the GUI through Protean's tool surface instead of shelling out.
    """
    import asyncio

    from mcp.server.stdio import stdio_server

    from protean.mcp import build_mcp_server
    from protean.platform import get_platform

    server_config = build_mcp_server(get_platform())
    server = server_config["instance"]
    init_options = server.create_initialization_options()

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)

    asyncio.run(_run())


@main.group()
def skills() -> None:
    """Manage skills."""
    pass


@skills.command("list")
@click.pass_context
def skills_list(ctx: click.Context) -> None:
    """List all available skills."""
    config: ProteanConfig = ctx.obj["config"]
    from protean.skills.registry import SkillRegistry

    registry = SkillRegistry(config.skills_dir)
    count = registry.load_all()

    if count == 0:
        click.echo("No skills found. Record a demonstration first:")
        click.echo("  protean record")
        return

    click.echo(f"Found {count} skill(s):\n")
    for skill in registry.list_skills():
        tags = ", ".join(skill.tags) if skill.tags else ""
        bins = ", ".join(skill.requires.bins) if skill.requires.bins else ""
        click.echo(f"  {skill.name}")
        click.echo(f"    {skill.description}")
        if bins:
            click.echo(f"    requires: {bins}")
        if tags:
            click.echo(f"    tags: {tags}")
        click.echo()


@skills.command("show")
@click.argument("name")
@click.pass_context
def skills_show(ctx: click.Context, name: str) -> None:
    """Show a skill's SKILL.md content."""
    config: ProteanConfig = ctx.obj["config"]
    from protean.skills.registry import SkillRegistry

    registry = SkillRegistry(config.skills_dir)
    registry.load_all()

    skill = registry.get(name)
    if skill is None:
        click.echo(f"Skill '{name}' not found.")
        sys.exit(1)

    click.echo(f"# {skill.name}")
    click.echo(f"> {skill.description}")
    if skill.requires.bins:
        click.echo(f"Requires: {', '.join(skill.requires.bins)}")
    if skill.goal:
        click.echo(f"\nGoal: {skill.goal}")
    if skill.steps:
        click.echo(f"\n## Steps ({len(skill.steps)})")
        for i, step in enumerate(skill.steps, 1):
            tool_tag = f" [{step.tool}]" if step.tool else ""
            click.echo(f"  {i}. {step.name}{tool_tag}")
            if step.verify_condition and step.verify_condition.description:
                click.echo(f"     Verify: {step.verify_condition.description}")
    elif skill.instructions:
        click.echo()
        click.echo(skill.instructions)


@skills.command("run")
@click.argument("name", required=False, default="")
@click.option(
    "--param", "-p", "params", multiple=True, metavar="KEY=VALUE",
    help="Parameter values for the skill, e.g. -p event_title=test",
)
@click.option(
    "--task", "-t", "task", default="",
    help="User task description, e.g. 'Check Server xin disk status'",
)
@click.option(
    "--provider", default=None,
    help="LLM provider for verification/refinement (openai/anthropic/gemini/doubao)",
)
@click.option("--model", "-m", default=None, help="Model name override")
@click.option(
    "--stepwise", "-s", is_flag=True,
    help="Execute and verify each step individually (default: full execution)",
)
@click.option(
    "--assist", "-a", is_flag=True,
    help="Interactively correct failed steps via CLI",
)
@click.option(
    "--refine", "-r", is_flag=True,
    help="Refine skill after execution using trajectory",
)
@click.option("--verbose", "-v", is_flag=True, help="Print executor events")
@click.option(
    "-E", "--executor", "executor_name",
    type=click.Choice(["computer_use", "claude_code"]),
    default="computer_use",
    show_default=True,
    help="Which executor backend to use",
)
@click.option(
    "--working-dir", "working_dir", default=None,
    help="Working dir for claude_code executor (default: skill directory)",
)
@click.option(
    "--overlay/--no-overlay", default=False, show_default=True,
    help="Show capture-proof overlay window",
)
@click.pass_context
def skills_run(
    ctx: click.Context, name: str, params: tuple[str, ...],
    task: str, provider: str | None, model: str | None,
    stepwise: bool, assist: bool, refine: bool, verbose: bool,
    executor_name: str, working_dir: str | None, overlay: bool,
) -> None:
    """Run a skill end-to-end via the executor."""
    config: ProteanConfig = ctx.obj["config"]

    from protean.executor import ExecutorEventType, get_executor_provider
    from protean.executor.providers.computer_use import _MAX_ITERATIONS
    from protean.overlay import enable_overlay_for_display, set_overlay_title, write_line
    from protean.platform import get_platform
    from protean.platform.base import active_display
    from protean.skills.registry import SkillRegistry
    from protean.skills.runner import RunMode, StepRunner

    # ── Look up skill (optional) ───────────────────────────
    skill = None
    skill_dir = None
    zero_shot = not name
    if not zero_shot:
        registry = SkillRegistry(config.skills_dir)
        registry.load_all()
        skill = registry.get(name)
        skill_dir = registry.get_path(name)

        if skill is None:
            click.echo(f"Skill '{name}' not found in {config.skills_dir}")
            sys.exit(1)

        click.echo(f"Skill: {skill.name}")
        click.echo(f"  {skill.description}")
        click.echo(f"  Steps: {len(skill.steps)}")
    elif not task.strip():
        click.echo("When skill name is omitted, --task is required.", err=True)
        sys.exit(1)

    if zero_shot and assist:
        click.secho("Warning: --assist is ignored when no skill is provided.", fg="yellow")
        assist = False

    # ── Parse --param KEY=VALUE ────────────────────────────
    param_values: dict[str, str] = {}
    for p in params:
        if "=" not in p:
            click.echo(f"Invalid param format '{p}', expected KEY=VALUE", err=True)
            sys.exit(1)
        k, v = p.split("=", 1)
        param_values[k.strip()] = v.strip()

    if param_values:
        click.echo(f"  Params: {param_values}")
        # Append parameter values to task so the executor knows them
        param_lines = "Parameter values: " + ", ".join(f"{k}={v}" for k, v in param_values.items())
        task = f"{task}\n{param_lines}" if task else f"{param_lines}"

    # ── Execute ────────────────────────────────────────────
    async def _run() -> None:
        from protean.llm import create_llm_from_config

        platform = get_platform()
        provider_name = provider or config.default_provider
        provider_cfg = config.llm_providers.get(provider_name, {})
        executor_model = model or provider_cfg.get("model", DEFAULT_MODEL_ANTHROPIC)
        if executor_name == "claude_code":
            cc_working_dir = working_dir or str(skill_dir) if skill_dir else working_dir
            executor = get_executor_provider(
                "claude_code",
                working_dir=cc_working_dir,
                platform=platform,
            )
        else:
            executor = get_executor_provider(
                "computer_use",
                api_key=provider_cfg.get("api_key", ""),
                model=executor_model,
                base_url=provider_cfg.get("base_url"),
                platform=platform,
                image_keep_last=config.image_keep_last,
                enable_terminal=config.cua_enable_terminal,
                mcp_terminal_command=config.cua_terminal_command,
            )
        llm = create_llm_from_config(config.llm_providers, config.default_provider, provider)

        def _update_overlay(evt: "ExecutorEvent") -> None:
            """Update overlay title and write event content."""
            if evt.type.value == "iteration":
                title = f"Iter {evt.message}"
                if evt.input_tokens or evt.output_tokens:
                    title += (
                        f" [{evt.input_tokens:.2E} in"
                        f" / {evt.output_tokens:.2E} out]"
                    )
                set_overlay_title(title)
                ts = time.strftime("%H:%M:%S")
                write_line(f"{'━' * 40}")
                write_line(f"{ts} Iteration {evt.message} / {_MAX_ITERATIONS}")
                write_line(f"{'━' * 40}")
            elif evt.type.value == "tool_call":
                write_line(f"→ {evt.tool_name} {evt.tool_args}")
            elif evt.type.value == "tool_result":
                result = evt.result[:200] if evt.result else ""
                write_line(f"  ✓ {result}")
            elif evt.type.value == "message":
                if evt.message:
                    write_line(f"  {evt.message}")
            elif evt.type.value == "error":
                write_line(f"✗ {evt.error}")
            elif evt.type.value == "done":
                set_overlay_title("Done")
                if evt.message:
                    write_line(f"── Done: {evt.message}")

        def _print_event(evt: "ExecutorEvent") -> None:
            _update_overlay(evt)
            if evt.type.value == "tool_call":
                click.echo(f"  [{evt.tool_name}] {evt.tool_args}")
            elif evt.type.value == "message":
                if evt.reasoning:
                    click.echo(f"  [reasoning] {evt.reasoning}")
                if evt.message:
                    click.echo(f"  {evt.message}")
            elif evt.type.value == "iteration":
                msg = evt.message
                if msg.startswith("Step "):
                    click.echo(f"\n{'=' * 50}")
                    click.echo(f"  {msg}")
                    click.echo(f"{'=' * 50}")
                else:
                    click.echo(f"\n── Iteration {msg} ──")
            elif evt.type.value == "done":
                click.echo("\n── Done ──")

        try:
            maybe_display = active_display(platform)
            overlay_display = maybe_display.display_index if maybe_display is not None else 1

            if zero_shot:
                click.echo("\nRunning zero-shot task...")
                cm = (
                    enable_overlay_for_display(display=overlay_display)
                    if overlay
                    else contextlib.nullcontext()
                )
                with cm:
                    await executor.start_task(task)
                    async for evt in executor.get_events():
                        _update_overlay(evt)
                        if verbose:
                            _print_event(evt)
                        if evt.type in (ExecutorEventType.DONE, ExecutorEventType.ERROR):
                            break
                return

            assert skill is not None

            skill_builder = None
            if refine or assist:
                from protean.skills.builder import SkillBuilder
                skill_builder = SkillBuilder()

            assistant = None
            if assist:
                from protean.channels.cli import CLIAssistantChannel
                assistant = CLIAssistantChannel(platform)

            from protean.skills.runner import ExecutionMode

            runner = StepRunner(
                executor, platform, llm,
                skill_builder=skill_builder,
                assistant=assistant,
                on_event=_print_event if verbose else None,
                execution_mode=(
                    ExecutionMode.STEP_BY_STEP if stepwise
                    else ExecutionMode.FULL
                ),
            )
            # Save original SKILL.md for diff if refining
            original_md = ""
            if refine and skill_dir:
                md_path = skill_dir / "SKILL.md"
                if md_path.exists():
                    original_md = md_path.read_text(encoding="utf-8")

            run_mode = RunMode.ASSISTED if assist else RunMode.VALIDATE
            mode_label = "stepwise" if stepwise else "full"
            if assist:
                mode_label += "+assisted"
            click.echo(f"\nRunning skill '{skill.name}' ({mode_label})...")
            cm = (
                enable_overlay_for_display(display=overlay_display)
                if overlay
                else contextlib.nullcontext()
            )
            with cm:
                report = await runner.run(skill, mode=run_mode, skill_dir=skill_dir, task=task)

            status = "PASSED" if report.passed else "FAILED"
            status_color = "green" if report.passed else "red"
            click.echo()
            click.echo("=" * 50)
            click.secho(f"  Result: {status}", fg=status_color, bold=True)
            click.echo(f"  Duration: {report.duration:.1f}s")
            if report.execution_result:
                click.echo(f"  {report.execution_result}")
            click.echo("=" * 50)

            # Show diff if skill was refined
            if refine and original_md and skill_dir:
                import difflib

                md_path = skill_dir / "SKILL.md"
                if md_path.exists():
                    new_md = md_path.read_text(encoding="utf-8")
                    if new_md != original_md:
                        diff = list(difflib.unified_diff(
                            original_md.splitlines(keepends=True),
                            new_md.splitlines(keepends=True),
                            fromfile="SKILL.md (before)",
                            tofile="SKILL.md (after)",
                        ))
                        click.echo()
                        click.secho("── Skill refined ──", bold=True)
                        for line in diff:
                            line = line.rstrip("\n")
                            if line.startswith("+++") or line.startswith("---"):
                                click.secho(line, bold=True)
                            elif line.startswith("+"):
                                click.secho(f"  ✚ {line[1:]}", fg="green")
                            elif line.startswith("-"):
                                click.secho(f"  ✖ {line[1:]}", fg="red")
                            elif line.startswith("@@"):
                                click.secho(line, fg="cyan", dim=True)
                            else:
                                click.echo(f"    {line[1:]}" if line.startswith(" ") else line)
                        synced = _sync_installed_agents(config)
                        if synced:
                            click.echo(f"\nSynced to: {', '.join(synced)}")
                    else:
                        click.echo("\n── No changes after refinement ──")
        finally:
            await executor.close()

    asyncio.run(_run())

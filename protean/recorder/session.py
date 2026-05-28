"""Recording session — orchestrates screen recording and input monitoring.

Dual-track recording:
  Track 1: Screen video via platform.start_screen_recording() (REQUIRED)
  Track 2: Input events (keyboard/mouse) via pynput → events.json

Output directory structure:
  <output_dir>/
    recording.mov          ← screen video (mandatory)
    events.json            ← all input events with window context

Video recording is mandatory. If screen recording permission is not granted,
we fail loudly and tell the user how to fix it — no silent fallbacks.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from protean.platform.base import DisplayInfo, Platform, get_platform
from protean.recorder.audio import AudioRecorder
from protean.recorder.events import InputEvent
from protean.recorder.input_monitor import InputMonitor
from protean.recorder.screenshotter import ScreenshotCapturer


@dataclass
class RecordingConfig:
    """Configuration for a recording session."""

    output_dir: Path
    display_index: int = 1  # 1-based display index (1 = primary)
    show_clicks: bool = True  # show click markers in video
    capture_audio: bool = False  # capture system audio (for calls)
    record_mouse_move: bool = False  # record mouse moves (verbose)
    record_audio: bool = True  # capture mic audio, run VAD + (mock) ASR, emit SPEECH events
    audio_device: int | str | None = None  # PortAudio device id/name; None = default mic
    screenshots: bool = True  # capture per-event screenshots alongside video


@dataclass
class RecordingResult:
    """Result of a completed recording session."""

    output_dir: Path
    events_file: Path
    video_file: Path | None
    duration: float  # seconds
    event_count: int
    display_index: int
    display_info: DisplayInfo | None
    summary: dict[str, int] = field(default_factory=dict)  # event_type -> count


def detect_active_display(platform: Platform) -> int:
    """Detect which display the cursor is currently on.

    Delegates to the shared implementation in platform.base.
    """
    from protean.platform.base import active_display_index

    return active_display_index(platform)


def list_displays_for_user(platform: Platform) -> list[dict[str, Any]]:
    """Return display info formatted for user display."""
    displays = platform.get_displays()
    result = []
    for d in displays:
        result.append(
            {
                "index": d.display_index,
                "resolution": f"{d.width}x{d.height}",
                "primary": d.is_primary,
                "scale": d.scale_factor,
            }
        )
    return result


class RecordingSession:
    """Orchestrates a complete dual-track recording session.

    Track 1: Screen video via screencapture (REQUIRED — fails if no permission)
    Track 2: Input events via pynput

    Usage:
        session = RecordingSession(config)
        session.start()
        # ... user performs the task ...
        result = session.stop()
    """

    def __init__(self, config: RecordingConfig, platform: Platform | None = None) -> None:
        self._config = config
        self._platform = platform or get_platform()
        self._events: list[InputEvent] = []
        self._lock = threading.Lock()
        self._input_monitor: InputMonitor | None = None
        self._start_time: float = 0
        self._running = False
        self._display_info: DisplayInfo | None = None
        self._video_path: Path | None = None
        self._capturer: ScreenshotCapturer | None = None
        self._audio_recorder: AudioRecorder | None = None
        self._audio_dir: Path | None = None
        self._journal_file: Any | None = None  # JSONL crash-recovery journal

    def start(self) -> dict[str, Any]:
        """Start recording. Returns a dict with all output paths for transparency.

        Raises RuntimeError if screen recording permission is not granted.
        """
        if self._running:
            raise RuntimeError("Recording already in progress")

        # Warn loudly when accessibility-tree prefetch is unavailable, so the
        # user notices before they record 10 minutes of element-less events.
        uia_msg = getattr(self._platform, "_uia_unavailable_reason", None)
        if uia_msg:
            import sys as _sys
            print(f"WARNING: {uia_msg}", file=_sys.stderr, flush=True)

        output_dir = self._config.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        self._events.clear()
        self._running = True

        # Resolve display
        displays = self._platform.get_displays()
        display_index = self._config.display_index
        self._display_info = None
        for d in displays:
            if d.display_index == display_index:
                self._display_info = d
                break

        # ── Track 1: Screen video recording ──
        video_path = output_dir / "recording.mov"
        self._video_path = video_path
        try:
            self._platform.start_screen_recording(
                video_path,
                display_index=display_index,
                show_clicks=self._config.show_clicks,
                capture_audio=self._config.capture_audio,
            )
            time.sleep(0.3)
            proc = getattr(self._platform, "_recording_process", None)
            if proc is not None:
                rc = proc.poll()
                if rc is not None:
                    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                    self._platform._recording_process = None
                    raise RuntimeError(
                        f"screencapture exited immediately (code {rc}): {err}"
                    )
            self._start_time = time.monotonic()
        except Exception as e:
            self._running = False
            hint = self._screen_recording_hint(e)
            raise RuntimeError(
                f"Failed to start screen recording: {e}\n\n{hint}\n\n"
                f"Then re-run: protean record"
            ) from e

        # ── Track 1b: Per-event screenshots (optional, alongside video) ──
        if self._config.screenshots:
            self._capturer = ScreenshotCapturer(
                self._platform,
                self._display_info,
                output_dir / "screenshots",
                display_index,
            )

        # ── Track 2: Input events (shares start_time with video) ──
        self._input_monitor = InputMonitor(
            self._platform,
            self._on_event,
            record_mouse_move=self._config.record_mouse_move,
        )
        # Open JSONL journal for crash recovery (append per-event)
        journal_path = output_dir / "events.journal.jsonl"
        self._journal_file = open(journal_path, "a", encoding="utf-8")  # noqa: SIM115
        self._input_monitor.start(self._start_time)

        # ── Track 3: Microphone audio + VAD + (mock) ASR ──
        if self._config.record_audio:
            self._audio_dir = output_dir / "audio"
            self._audio_recorder = AudioRecorder(
                self._on_event,
                self._audio_dir,
                device=self._config.audio_device,
            )
            self._audio_recorder.start(self._start_time)

        # Return all paths for transparency
        return {
            "output_dir": str(output_dir),
            "video_file": str(video_path),
            "screenshots_dir": str(output_dir / "screenshots") if self._config.screenshots else None,
            "audio_dir": str(self._audio_dir) if self._audio_dir else None,
            "events_file": str(output_dir / "events.json"),
            "display_index": display_index,
            "display_info": {
                "resolution": f"{self._display_info.width}x{self._display_info.height}",
                "primary": self._display_info.is_primary,
            }
            if self._display_info
            else None,
            "all_displays": list_displays_for_user(self._platform),
        }

    def stop(self) -> RecordingResult:
        """Stop recording and return the result."""
        if not self._running:
            raise RuntimeError("No recording in progress")

        duration = time.monotonic() - self._start_time

        # Stop input monitor first (so we don't miss final events)
        # Note: _running stays True until monitor is stopped, so late events
        # from pynput threads are still accepted during shutdown.
        if self._audio_recorder:
            # Stop audio first so any in-progress utterance is finalised and
            # delivered through _on_event before we tear down the monitor.
            self._audio_recorder.stop()
        if self._input_monitor:
            self._input_monitor.stop()

        self._running = False

        # Close crash-recovery journal
        if self._journal_file is not None:
            try:
                self._journal_file.close()
            except Exception:
                pass
            self._journal_file = None

        # Stop video recording
        video_file = self._platform.stop_screen_recording()
        if video_file is None:
            video_file = self._video_path

        # Save events
        events_file = self._config.output_dir / "events.json"
        self._save_events(events_file, duration)

        # Remove crash-recovery journal now that canonical file is written
        journal_path = self._config.output_dir / "events.journal.jsonl"
        try:
            journal_path.unlink(missing_ok=True)
        except Exception:
            pass

        # Build summary
        summary: dict[str, int] = {}
        for event in self._events:
            key = event.event_type.value
            summary[key] = summary.get(key, 0) + 1

        return RecordingResult(
            output_dir=self._config.output_dir,
            events_file=events_file,
            video_file=video_file,
            duration=duration,
            event_count=len(self._events),
            display_index=self._config.display_index,
            display_info=self._display_info,
            summary=summary,
        )

    @staticmethod
    def _screen_recording_hint(exc: BaseException) -> str:
        """Build an OS-aware hint for screen-recording startup failures."""
        import sys

        msg = str(exc)
        is_missing_binary = (
            isinstance(exc, FileNotFoundError)
            or "WinError 2" in msg
            or "No such file or directory" in msg
            or "command not found" in msg
        )
        if sys.platform == "win32":
            if is_missing_binary:
                return (
                    "ffmpeg was not found on PATH. Install it (e.g. "
                    "`winget install Gyan.FFmpeg` or "
                    "`choco install ffmpeg`) and reopen the terminal so PATH refreshes."
                )
            return (
                "Check that ffmpeg can capture the desktop "
                "(try: ffmpeg -f gdigrab -framerate 30 -i desktop -frames:v 1 out.png)."
            )
        if sys.platform == "darwin":
            return (
                "On macOS, grant Screen Recording permission:\n"
                "  System Settings → Privacy & Security → Screen Recording\n"
                "  → Enable for your terminal app (Terminal / iTerm2 / etc.)"
            )
        # linux / other
        if is_missing_binary:
            return "ffmpeg was not found on PATH. Install it via your package manager."
        return "Check screen-capture permissions and ffmpeg/x11grab availability."

    def _on_event(self, event: InputEvent) -> None:
        """Callback for incoming events."""
        # v2: capture screenshot for salient events before storing
        if self._capturer is not None and self._capturer.should_capture(event):
            try:
                self._capturer.capture_for(event)
            except Exception:
                pass
        with self._lock:
            self._events.append(event)
            if self._journal_file is not None:
                try:
                    self._journal_file.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                    self._journal_file.flush()
                except Exception:
                    pass

    def _save_events(self, path: Path, duration: float) -> None:
        """Save all events to a JSON file."""
        with self._lock:
            events_data = [e.to_dict() for e in self._events]

        data: dict[str, Any] = {
            "version": "0.2.0",
            "duration": round(duration, 3),
            "event_count": len(events_data),
            "display_index": self._config.display_index,
            "display": {
                "width": self._display_info.width,
                "height": self._display_info.height,
                "primary": self._display_info.is_primary,
                "origin_x": self._display_info.origin_x,
                "origin_y": self._display_info.origin_y,
                "scale_factor": self._display_info.scale_factor,
            }
            if self._display_info
            else None,
            "events": events_data,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

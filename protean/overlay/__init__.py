"""Capture-proof log overlay window.

Spawns a subprocess with a tkinter window that displays log lines in real-time.
The window is invisible to all screen capture APIs (screencapture, mss, dxcam)
so the CUA executor never sees it.

Usage:
    from protean.overlay import enable_overlay_for_display

    # Start overlay for display 1, with logging handler attached
    with enable_overlay_for_display(display_index=1):
        # Run executor here — logs will appear in overlay
        await executor.start_task(...)
"""

import atexit
import contextlib
import logging
import subprocess
import sys
from typing import Generator

log = logging.getLogger(__name__)

process: subprocess.Popen[str] | None = None
display_index: int = 1  # Track which display the overlay is on


def start_overlay(display: int = 1) -> None:
    """Spawn the overlay subprocess on the specified display."""
    global process, display_index
    if process is not None and process.poll() is None:
        return
    display_index = display
    process = subprocess.Popen(
        [sys.executable, "-m", "protean.overlay.window", str(display)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    atexit.register(stop_overlay)
    log.debug("Overlay started (pid=%d, display=%d)", process.pid, display)


def stop_overlay() -> None:
    """Shut down the overlay subprocess."""
    global process
    if process is None:
        return
    proc = process
    process = None
    try:
        proc.stdin.close()  # type: ignore[union-attr]
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.debug("Overlay stopped")


def write_line(line: str) -> None:
    """Send a single line to the overlay. No-op if overlay is not running."""
    proc = process
    if proc is None or proc.stdin is None or proc.poll() is not None:
        return
    try:
        proc.stdin.write(line.rstrip("\n") + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass


def set_overlay_title(title: str) -> None:
    """Update the overlay window title. No-op if overlay is not running."""
    write_line(f"@@TITLE:{title}")


# Only show logs from these loggers (executor events, tool handlers).
# Excludes httpx, httpcore, openai, anthropic, urllib3, etc.
_OVERLAY_LOGGER_PREFIXES = (
    "protean.executor",
    "protean.realtime",
    "protean.skills",
)


class OverlayLogHandler(logging.Handler):
    """Logging handler that routes formatted records to the overlay window.

    Filters to only show executor/tool logs, not HTTP library noise.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Filter: only pass through protean executor/skill logs
        if not record.name.startswith(_OVERLAY_LOGGER_PREFIXES):
            return
        try:
            write_line(self.format(record))
        except Exception:
            self.handleError(record)


@contextlib.contextmanager
def enable_overlay_for_display(display: int = 1) -> Generator[None, None, None]:
    """Context manager to start/stop the overlay window.

    Events are written explicitly via write_line() and set_overlay_title()
    from the CLI event consumer — no log handler needed.

    Usage:
        with enable_overlay_for_display(display_index=1):
            # Overlay runs during this block
            await executor.start_task(...)
    """
    start_overlay(display)
    try:
        yield
    finally:
        stop_overlay()

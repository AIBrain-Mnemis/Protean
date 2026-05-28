"""CLI assistant channel — prompt the operator via macOS dialog or terminal.

Implements the AssistantChannel protocol using Platform.prompt_text()
for a native macOS input dialog, falling back to asyncio stdin if
the dialog is unavailable or times out.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from protean.channels.base import AssistantChannel

if TYPE_CHECKING:
    from protean.platform.base import Platform

log = logging.getLogger(__name__)


class CLIAssistantChannel(AssistantChannel):
    """AssistantChannel backed by a macOS text prompt with terminal fallback."""

    def __init__(self, platform: Platform) -> None:
        self._platform = platform

    # ── AssistantChannel protocol ─────────────────────────────

    async def ask(self, question: str, screenshot: bytes | None = None) -> str:
        """Ask a free-form question. Blocks until the human responds."""
        screenshot_path = await self._show_screenshot(screenshot) if screenshot else None
        # Split on first newline: short title for title bar, rest as body
        if "\n" in question:
            title, message = question.split("\n", 1)
        else:
            title, message = question, ""
        try:
            answer = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._platform.prompt_text(
                    title=title.strip(),
                    placeholder="Type your answer here...",
                    message=message.strip(),
                ),
            )
            return answer or ""
        finally:
            if screenshot_path:
                screenshot_path.unlink(missing_ok=True)

    async def confirm(self, message: str) -> bool:
        """Ask a yes/no question. Returns True for yes."""
        response = await self.ask(f"{message} (yes/no)")
        return response.strip().lower() in {"yes", "y", "true", "1"}

    # ── Internals ───────────────────────────────────────────

    async def _show_screenshot(self, screenshot: bytes) -> Path | None:
        """Save screenshot to a temp file and open it. Returns path for caller to clean up."""
        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".png", prefix="protean_screenshot_", delete=False,
            )
            tmp.write(screenshot)
            tmp.close()
            path = Path(tmp.name)
            log.info("Screenshot saved to %s", path)
            proc = await asyncio.create_subprocess_exec(
                "open", str(path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return path
        except Exception:
            log.warning("Failed to show screenshot", exc_info=True)
            return None


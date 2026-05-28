"""Smoke test: LLM sees a visual dialog triggered by run_terminal_command.

Runs a script that opens a macOS dialog (osascript) asking for a secret.
The LLM must:
  1. See the dialog in the screenshot returned by run_terminal_command
  2. Type "protean" into the dialog's text field
  3. Click OK
  4. Observe that the process outputs "SUCCESS"

Requires:
  - macOS (uses osascript)
  - Real LLM API key (ANTHROPIC_API_KEY or config)
  - Display (screenshot must capture the dialog)

Run:
    uv run python tests/smoke_terminal_cua.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from time import time

import click

from protean.config import ProteanConfig
from protean.executor import ExecutorEvent, ExecutorEventType, get_executor_provider
from protean.platform import get_platform

_SCRIPT = os.path.join(os.path.dirname(__file__), "_dialog_prompt_script.py")


def _print_event(evt: ExecutorEvent) -> None:
    t = evt.type.value
    if t == "iteration":
        click.echo(f"\n── Iteration {evt.message} ──")
    elif t == "tool_call":
        click.echo(f"  [{evt.tool_name}] {evt.tool_args}")
    elif t == "message":
        if evt.reasoning:
            click.echo(f"  [reasoning] {evt.reasoning[:200]}")
        if evt.message:
            click.echo(f"  {evt.message}")
    elif t == "done":
        click.echo("\n── Done ──")
        click.echo(evt.message)
    elif t == "error":
        click.echo(f"  ERROR: {evt.error}")


async def main() -> None:
    config = ProteanConfig.load()
    platform = get_platform()
    model_cfg = config.llm_providers["anthropic"]

    executor = get_executor_provider(
        "computer_use",
        api_key=model_cfg["api_key"],
        model=model_cfg.get("model"),
        base_url=model_cfg.get("base_url"),
        platform=platform,
        image_keep_last=10,
    )

    task = (
        f"Run the script at {_SCRIPT} using run_terminal_command. "
        f"If it asks for authentication, the password is 'protean'. "
        f"Verify the output says SUCCESS."
    )

    click.echo(f"Task: {task}")
    click.echo("Starting executor...")

    await executor.start_task(task)

    success = False
    async for evt in executor.get_events():
        _print_event(evt)
        if evt.type == ExecutorEventType.DONE:
            if "SUCCESS" in evt.message:
                success = True

    await executor.close()

    if success:
        click.echo("\n✓ PASSED: LLM saw the dialog, typed the secret, got SUCCESS")
    else:
        click.echo("\n✗ FAILED: LLM did not achieve SUCCESS")
        sys.exit(1)


if __name__ == "__main__":
    start = time()
    asyncio.run(main())
    click.echo(f"\nTotal execution time: {time() - start:.2f} seconds")

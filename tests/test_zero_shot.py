"""Zero-shot task: hand a natural-language instruction straight to the executor.

No recording, no Skill, no verifier — just the ExecutorProvider loop.
Proves that the existing executor infrastructure can drive the desktop
from a single prompt.

Run:
    uv run python tests/test_zero_shot.py
    uv run python tests/test_zero_shot.py -E claude_code
    uv run python tests/test_zero_shot.py -t "Open Safari and search for 'protean github'"
"""

from __future__ import annotations

import asyncio
import sys
from time import time

import click

from protean.config import ProteanConfig
from protean.executor import ExecutorEvent, ExecutorEventType, get_executor_provider
from protean.platform import get_platform

_DEFAULT_TASK = (
    "Open the macOS Calculator app, compute 123 + 456, "
    "and report the result shown on the display."
)


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


async def run_zero_shot(task: str, executor_name: str) -> str:
    config = ProteanConfig.load()
    platform = get_platform()

    if executor_name == "claude_code":
        executor = get_executor_provider("claude_code")
    else:
        provider_cfg = config.llm_providers[config.default_provider]
        executor = get_executor_provider(
            "computer_use",
            api_key=provider_cfg["api_key"],
            model=provider_cfg.get("model"),
            base_url=provider_cfg.get("base_url"),
            platform=platform,
            image_keep_last=config.image_keep_last,
            enable_terminal=config.cua_enable_terminal,
            mcp_terminal_command=config.cua_terminal_command,
        )

    click.echo(f"Executor: {executor_name}")
    click.echo(f"Task:     {task}")
    click.echo("Starting...\n")

    await executor.start_task(task)

    final_message = ""
    try:
        async for evt in executor.get_events():
            _print_event(evt)
            if evt.type == ExecutorEventType.DONE:
                final_message = evt.message
    finally:
        await executor.close()

    return final_message


@click.command()
@click.option("-t", "--task", default=_DEFAULT_TASK, help="Natural-language task")
@click.option(
    "-E", "--executor", "executor_name",
    type=click.Choice(["computer_use", "claude_code"]),
    default="computer_use", show_default=True,
)
def main(task: str, executor_name: str) -> None:
    start = time()
    result = asyncio.run(run_zero_shot(task, executor_name))
    click.echo(f"\nTotal: {time() - start:.1f}s")
    if not result:
        sys.exit(1)


if __name__ == "__main__":
    main()

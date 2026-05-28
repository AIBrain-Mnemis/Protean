"""Direct CUA executor test — runs a skill via StepRunner with verbose output.

Usage:
    uv --directory ... run python tests/test_cua.py
"""

import asyncio
import sys
from time import time

import click

from protean.config import ProteanConfig
from protean.executor import ExecutorEvent, get_executor_provider
from protean.platform import get_platform


def _print_event(evt: ExecutorEvent) -> None:
    """Verbose event printer matching CLI --verbose output."""
    t = evt.type.value
    if t == "iteration":
        click.echo(f"\n── Iteration {evt.message} ──")
    elif t == "tool_call":
        click.echo(f"  [{evt.tool_name}] {evt.tool_args}")
    elif t == "message":
        if evt.reasoning:
            click.echo(f"  [reasoning] {evt.reasoning}")
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
    # model_cfg = config.llm_providers["openai"]

    executor = get_executor_provider(
        "computer_use",
        api_key=model_cfg["api_key"],
        model=model_cfg.get("model"),
        base_url=model_cfg.get("base_url"),
        platform=platform,
        image_keep_last=10,
    )

    # task = (
    #     "帮我在outlook上预定一个今天22:00-22:30的会议，邀请Zihao Tang，"
    #     "在BJW-2 7478会议室，同时开启teams会议。"
    #     "严格执行操作，不必在意他的忙闲。"
    # )
    # skill_name = "schedule-outlook-meeting"

    task = "Check Server xin's disk status."
    skill_name = "check-xin-disk-status"

    from protean.llm import create_llm_from_config
    from protean.skills.registry import SkillRegistry
    from protean.skills.runner import RunMode, StepRunner

    registry = SkillRegistry(config.skills_dir)
    registry.load_all()
    skill = registry.get(skill_name)

    if not skill:
        click.echo(f"Skill '{skill_name}' not found")
        sys.exit(1)

    click.echo(f"Skill: {skill.name} ({len(skill.steps)} steps)")

    llm = create_llm_from_config(config.llm_providers, config.default_provider)
    runner = StepRunner(executor, platform, llm, on_event=_print_event)

    try:
        report = await runner.run(skill, mode=RunMode.VALIDATE, task=task)
        status = "PASSED" if report.passed else "FAILED"
        click.echo(f"\nResult: {status}")
        click.echo(f"Duration: {report.duration:.1f}s")
        if report.execution_result:
            click.echo(f"\nExecution output:\n{report.execution_result}")
    finally:
        await executor.close()


if __name__ == "__main__":
    start = time()
    asyncio.run(main())
    end = time()
    click.echo(f"\nTotal execution time: {end - start:.2f} seconds")

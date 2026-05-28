"""Force the CUA to use run_terminal_command — no skill, no GUI distractions.

Run:
    python tests/smoke_terminal_via_cua.py
"""

from __future__ import annotations

import asyncio

from protean.config import ProteanConfig
from protean.executor import ExecutorEvent, get_executor_provider
from protean.platform import get_platform


def _print(evt: ExecutorEvent) -> None:
    t = evt.type.value
    if t == "tool_call":
        print(f"  [{evt.tool_name}] {evt.tool_args}")
    elif t == "message" and evt.message:
        print(f"  msg: {evt.message[:300]}")
    elif t == "iteration":
        print(f"\n── iter {evt.message} ──")
    elif t == "done":
        print(f"\n── done ──\n{evt.message}")
    elif t == "error":
        print(f"  ERROR: {evt.error}")


async def main() -> None:
    config = ProteanConfig.load()
    platform = get_platform()
    cfg = config.llm_providers["anthropic"]

    executor = get_executor_provider(
        "computer_use",
        api_key=cfg["api_key"],
        model=cfg.get("model"),
        base_url=cfg.get("base_url"),
        platform=platform,
        enable_terminal=True,
        max_iterations=5,
    )

    task = (
        "Use the run_terminal_command tool to execute "
        "'dir C:\\Users\\wuqi\\Projects\\Protean'. "
        "Read the output, summarize what you see, then call done. "
        "Do NOT take screenshots or click anything — terminal only."
    )

    try:
        await executor.start_task(task)
        async for evt in executor.get_events():
            _print(evt)
    finally:
        await executor.close()


if __name__ == "__main__":
    asyncio.run(main())

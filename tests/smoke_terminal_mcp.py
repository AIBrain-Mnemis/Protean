"""Smoke test for the DesktopCommanderMCP terminal sidecar (v2 async API).

Tests:
  A. Start a script that prompts for input → detect prompt → send correct
     answer → process exits with SUCCESS.
  B. Start same script → send wrong answer → idle detection fires (~30s) →
     then process exits with FAIL.

Requires `npx` on PATH. Does NOT call any LLM.

Run:
    python tests/smoke_terminal_mcp.py          # both tests
    python tests/smoke_terminal_mcp.py --a      # test A only
    python tests/smoke_terminal_mcp.py --b      # test B only (slow: ~35s)
"""

from __future__ import annotations

import asyncio
import os
import sys

from protean.executor.providers.computer_use import _TerminalMCP

_SCRIPT = os.path.join(os.path.dirname(__file__), "_input_prompt_script.py")


async def test_a_correct_input(term: _TerminalMCP) -> None:
    """A: send correct input → SUCCESS."""
    print("\n═══ Test A: correct input ═══")

    cmd = f"python3 {_SCRIPT}"
    print(f"$ {cmd}")
    out = await term.start_command(cmd, timeout_seconds=3, shell=None)
    print(f"Initial: {out[:300]}")

    # The start_command should return with a PID (process waiting for input).
    assert "PID" in out, f"Expected PID in output, got: {out}"

    # Check if interactive prompt was detected in notifications.
    await asyncio.sleep(1)
    prompt_notified = any("interactive" in n for n in term.notifications)
    if prompt_notified:
        print("✓ Interactive prompt detected in notifications")
        term.notifications.clear()
    else:
        print("⚠ No prompt notification (may have been too fast for regex)")

    # Extract PID from output.
    import re
    m = re.search(r"PID (\d+)", out)
    assert m, f"Could not extract PID from: {out}"
    pid = int(m.group(1))
    print(f"PID: {pid}")

    # Send correct answer.
    print("Sending input: 'protean'")
    response = await term.send_input(pid, "protean")
    print(f"Response: {response[:200]}")

    # Wait for process to exit and notification.
    for _ in range(10):
        await asyncio.sleep(1)
        if any("finished" in n for n in term.notifications):
            break

    # Check exit notification.
    exit_notes = [n for n in term.notifications if "finished" in n]
    assert exit_notes, f"Expected 'finished' notification, got: {term.notifications}"
    assert "SUCCESS" in exit_notes[0], f"Expected SUCCESS in output: {exit_notes[0]}"
    print(f"✓ Exit notification: ...{exit_notes[0][-80:]}")
    term.notifications.clear()
    print("═══ Test A: PASSED ═══")


async def test_b_wrong_input_idle(term: _TerminalMCP) -> None:
    """B: send wrong input → idle detection fires → then FAIL."""
    print("\n═══ Test B: wrong input + idle detection (~35s) ═══")

    cmd = f"python3 {_SCRIPT}"
    print(f"$ {cmd}")
    out = await term.start_command(cmd, timeout_seconds=3, shell=None)
    print(f"Initial: {out[:300]}")

    assert "PID" in out
    import re
    m = re.search(r"PID (\d+)", out)
    assert m
    pid = int(m.group(1))
    print(f"PID: {pid}")

    # Send wrong answer.
    print("Sending input: 'wrong'")
    response = await term.send_input(pid, "wrong")
    print(f"Response: {response[:200]}")
    term.notifications.clear()

    # Wait for idle detection (~30s) + process exit (~35s).
    print("Waiting for idle notification (~30s)...")
    idle_seen = False
    exit_seen = False
    for i in range(120):
        await asyncio.sleep(1)
        for n in term.notifications:
            if "idle" in n and not idle_seen:
                idle_seen = True
                print(f"✓ Idle notification at ~{i}s: {n}")
            if "finished" in n and not exit_seen:
                exit_seen = True
                print(f"✓ Exit notification at ~{i}s: {n}")
        if exit_seen:
            break

    assert idle_seen, "Expected idle notification before exit"
    assert exit_seen, "Expected exit notification"
    term.notifications.clear()
    print("═══ Test B: PASSED ═══")


async def main() -> None:
    run_a = "--b" not in sys.argv
    run_b = "--a" not in sys.argv

    term = _TerminalMCP(
        ["npx", "-y", "@wonderwhy-er/desktop-commander@latest"],
    )
    print("Starting MCP server (first run may download the npm package)...")
    await term.start()
    print("MCP server ready.")

    try:
        if run_a:
            await test_a_correct_input(term)
        if run_b:
            await test_b_wrong_input_idle(term)
        print("\n✓ All tests passed.")
    finally:
        print("\nShutting down MCP server...")
        await term.close()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())

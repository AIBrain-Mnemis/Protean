"""Manual test for find_elements — matches executor output.

Usage:
    uv run python tests/test_find_elements.py "Microsoft Teams" "发送"
    uv run python tests/test_find_elements.py "Microsoft Teams" "chat"
    uv run python tests/test_find_elements.py "访达" "ssh"
"""

from __future__ import annotations

import sys

from protean.platform import get_platform
from protean.realtime.tool_handlers import execute_tool


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <app> <query>")
        sys.exit(1)

    app = sys.argv[1]
    query = sys.argv[2]
    p = get_platform()

    p.activate_app(app)
    import time
    time.sleep(2)

    result = execute_tool(p, "find_elements", {"app": app, "query": query})
    print(result)


if __name__ == "__main__":
    main()

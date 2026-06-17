"""Manual test for Platform.find_elements.

Usage:
    uv run python tests/test_find_elements.py "Microsoft Teams" "发送"
    uv run python tests/test_find_elements.py "Microsoft Teams" "chat"
    uv run python tests/test_find_elements.py "访达" "ssh"
"""

from __future__ import annotations

import sys
import time

from protean.platform import get_platform


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <app> <query>")
        sys.exit(1)

    app = sys.argv[1]
    query = sys.argv[2]
    p = get_platform()

    p.activate_app(app)
    time.sleep(2)

    results = p.find_elements(app, query)
    if not results:
        print(f"No elements matching {query!r} found in {app}")
        return
    for i, el in enumerate(results, 1):
        label = el.label if len(el.label) <= 80 else el.label[:77] + "..."
        print(
            f"{i}. {el.role}: {label!r} "
            f"center=({el.center_x},{el.center_y}) "
            f"size={el.width}x{el.height}"
        )


if __name__ == "__main__":
    main()

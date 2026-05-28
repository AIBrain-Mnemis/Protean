"""Manual test for Platform.element_at — run interactively.

Usage:
    uv run python tests/test_element_at.py

Hover over different UI elements and watch the output.
Press Ctrl+C to stop.
"""

from __future__ import annotations

import time

from protean.platform import get_platform


def main() -> None:
    p = get_platform()
    print("element_at test — hover over UI elements. Ctrl+C to stop.\n")

    prev = ""
    while True:
        x, y = p.get_cursor_position()
        info = p.element_at(x, y)
        if info is None:
            line = f"({x:4d}, {y:4d}) — no element"
        else:
            line = (
                f"({x:4d}, {y:4d}) → {info.role}: {info.label!r} "
                f"center=({info.center_x}, {info.center_y}) "
                f"size={info.width}x{info.height}"
            )
        if line != prev:
            print(line)
            prev = line
        time.sleep(0.2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDone.")

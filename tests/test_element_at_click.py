"""Manual test: click anywhere to inspect element + its AX tree ancestry.

Usage:
    uv run python tests/test_element_at_click.py

Click on any UI element. Prints:
  - The element at the click point (role, label, position)
  - Its parent chain up to the application root

Press Ctrl+C to stop.
"""

from __future__ import annotations

from protean.platform import get_platform


def _get_element_attrs(el) -> dict[str, str]:
    """Extract key attributes from an AXUIElement."""
    from ApplicationServices import AXUIElementCopyAttributeValue

    attrs = {}
    _, role = AXUIElementCopyAttributeValue(el, "AXRole", None)
    attrs["role"] = str(role) if role else ""
    for key in (
        "AXTitle", "AXDescription", "AXValue", "AXRoleDescription",
        "AXIdentifier", "AXDOMIdentifier", "AXDOMClassList",
        "AXSubrole", "AXHelp",
    ):
        try:
            _, val = AXUIElementCopyAttributeValue(el, key, None)
        except Exception:
            continue
        if val:
            attrs[key] = str(val)[:120]
    return attrs


def _walk_parents(el) -> list[dict[str, str]]:
    """Walk the AX parent chain from element to root."""
    from ApplicationServices import AXUIElementCopyAttributeValue

    chain = [_get_element_attrs(el)]
    current = el
    for _ in range(30):  # safety limit
        try:
            _, parent = AXUIElementCopyAttributeValue(current, "AXParent", None)
        except Exception:
            break
        if parent is None:
            break
        chain.append(_get_element_attrs(parent))
        current = parent
    return chain


def _format_attrs(attrs: dict[str, str]) -> str:
    role = attrs.get("role", "?")
    parts = [role]
    for key in (
        "AXTitle", "AXDescription", "AXValue", "AXRoleDescription",
        "AXIdentifier", "AXDOMIdentifier", "AXDOMClassList",
        "AXSubrole", "AXHelp",
    ):
        if key in attrs:
            parts.append(f"{key}={attrs[key]!r}")
    return " | ".join(parts)


def main() -> None:
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCopyElementAtPosition,
        AXUIElementCreateSystemWide,
    )
    from Quartz import (
        CFMachPortCreateRunLoopSource,
        CFRunLoopAddSource,
        CFRunLoopGetCurrent,
        CFRunLoopRun,
        CGEventGetLocation,
        CGEventMaskBit,
        CGEventTapCreate,
        kCFRunLoopCommonModes,
        kCGEventLeftMouseDown,
        kCGHeadInsertEventTap,
        kCGSessionEventTap,
    )

    from protean.platform.macos import _extract_ax_point, _extract_ax_size

    _ = get_platform()
    system = AXUIElementCreateSystemWide()

    print("Click on any element to inspect. Ctrl+C to stop.\n")

    def _on_click(_proxy, _type, event, _refcon):
        loc = CGEventGetLocation(event)
        x, y = int(loc.x), int(loc.y)

        err, el = AXUIElementCopyElementAtPosition(system, float(x), float(y), None)
        if err != 0 or el is None:
            print(f"\n--- Click at ({x}, {y}) — no element found ---\n")
            return event

        # Element info
        _, pos_val = AXUIElementCopyAttributeValue(el, "AXPosition", None)
        _, size_val = AXUIElementCopyAttributeValue(el, "AXSize", None)
        px, py = _extract_ax_point(pos_val)
        w, h = _extract_ax_size(size_val)

        attrs = _get_element_attrs(el)
        size_str = f" size={int(w)}x{int(h)}" if w is not None else ""
        pos_str = f" pos=({int(px)},{int(py)})" if px is not None else ""

        print(f"\n{'='*60}")
        print(f"Click at ({x}, {y})")
        print(f"Element: {_format_attrs(attrs)}{pos_str}{size_str}")

        # Parent chain
        chain = _walk_parents(el)
        if len(chain) > 1:
            print(f"\nParent chain ({len(chain)-1} levels):")
            for i, node in enumerate(chain[1:], 1):
                indent = "  " * i
                print(f"{indent}↑ {_format_attrs(node)}")

        print(f"{'='*60}")
        return event

    tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        0,
        CGEventMaskBit(kCGEventLeftMouseDown),
        _on_click,
        None,
    )
    if tap is None:
        print("ERROR: Failed to create event tap. Grant Accessibility permission.")
        return

    source = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
    CFRunLoopRun()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDone.")

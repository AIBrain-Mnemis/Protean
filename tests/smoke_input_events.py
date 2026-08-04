"""End-to-end input-simulation test.

Detects the active Platform backend and exercises EVERY supported
key_press / type_text / mouse operation against a live OS-level input
listener (pynput). No GUI window is needed — pynput observes events at
the HID layer globally.

Run:
    uv run python tests/smoke_input_events.py
    uv run python tests/smoke_input_events.py --only keys
    uv run python tests/smoke_input_events.py --only mouse
    uv run python tests/smoke_input_events.py --only text

Requires:
    - focused desktop session (don't run over SSH)
    - pynput (already a project dependency — see pyproject.toml)
    - OS permission:
        * macOS: Accessibility AND Input Monitoring granted to the
                 Python binary / Terminal. Both simulation and the
                 global listener require it.
        * Windows: run from a normal (non-elevated) desktop session;
                   input is blocked against elevated windows if this
                   process is non-elevated.

NOTE on type_text:
    type_text is implemented via clipboard + paste (Cmd+V / Ctrl+V).
    We verify two things without focusing any app:
      1. clipboard now contains the exact sample text, and
      2. the paste hot-key was observed by the listener.
    We do NOT focus a text field because that would route the paste into
    whatever app you happen to have open. The hot-key + clipboard check
    is sufficient to prove type_text did its job.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# Listener wrapping pynput
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class KeyEvent:
    key_repr: str      # e.g. "a", "Key.cmd", "Key.enter"
    is_modifier: bool


@dataclass
class MouseClickEvent:
    x: int
    y: int
    button: str
    pressed: bool


@dataclass
class MouseMoveEvent:
    x: int
    y: int


@dataclass
class MouseScrollEvent:
    x: int
    y: int
    dx: int
    dy: int


class InputListener:
    """Global keyboard + mouse listener with a simple clear()/read() cursor."""

    def __init__(self) -> None:
        from pynput import keyboard, mouse

        self._keyboard_mod = keyboard
        self._mouse_mod = mouse

        self._lock = threading.Lock()
        self._key_events: list[KeyEvent] = []
        self._mouse_events: list[object] = []
        self._key_cursor = 0
        self._mouse_cursor = 0

        self._kb_listener = keyboard.Listener(on_press=self._on_press)
        self._mouse_listener = mouse.Listener(
            on_click=self._on_click,
            on_move=self._on_move,
            on_scroll=self._on_scroll,
        )

    def _key_repr(self, key: object) -> tuple[str, bool]:
        KeyBase = self._keyboard_mod.Key
        KeyCodeBase = self._keyboard_mod.KeyCode
        if isinstance(key, KeyBase):
            return (f"Key.{key.name}", True)
        if isinstance(key, KeyCodeBase):
            if key.char is not None:
                return (key.char, False)
            if key.vk is not None:
                return (f"vk={key.vk}", False)
        return (str(key), False)

    def _on_press(self, key: object) -> None:
        rep, is_mod = self._key_repr(key)
        with self._lock:
            self._key_events.append(KeyEvent(rep, is_mod))

    def _on_click(self, x: float, y: float, button: object, pressed: bool) -> None:
        name = getattr(button, "name", str(button))
        with self._lock:
            self._mouse_events.append(
                MouseClickEvent(int(x), int(y), str(name), bool(pressed))
            )

    def _on_move(self, x: float, y: float) -> None:
        with self._lock:
            self._mouse_events.append(MouseMoveEvent(int(x), int(y)))

    def _on_scroll(self, x: float, y: float, dx: float, dy: float) -> None:
        with self._lock:
            self._mouse_events.append(
                MouseScrollEvent(int(x), int(y), int(dx), int(dy))
            )

    def start(self) -> None:
        self._kb_listener.start()
        self._mouse_listener.start()
        time.sleep(0.5)  # let listeners attach

    def stop(self) -> None:
        try:
            self._kb_listener.stop()
        except Exception:
            pass
        try:
            self._mouse_listener.stop()
        except Exception:
            pass

    def clear(self) -> None:
        with self._lock:
            self._key_cursor = len(self._key_events)
            self._mouse_cursor = len(self._mouse_events)

    def read_keys(self, wait: float = 0.35) -> list[KeyEvent]:
        time.sleep(wait)
        with self._lock:
            return list(self._key_events[self._key_cursor:])

    def read_mouse(self, wait: float = 0.35) -> list[object]:
        time.sleep(wait)
        with self._lock:
            return list(self._mouse_events[self._mouse_cursor:])


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"  \u2713 {name}")

    def fail(self, name: str, reason: str) -> None:
        self.failed.append((name, reason))
        print(f"  \u2717 {name} \u2014 {reason}")


# ─────────────────────────────────────────────────────────────────────────────
# Expectation tables
# ─────────────────────────────────────────────────────────────────────────────

# Trigger-key name we pass to platform.key_press  →  canonical pynput repr
_KEYS_COMMON: dict[str, str] = {
    "return":     "Key.enter",
    "enter":      "Key.enter",
    "tab":        "Key.tab",
    "escape":     "Key.esc",
    "esc":        "Key.esc",
    "backspace":  "Key.backspace",
    "space":      "Key.space",
    "up":         "Key.up",
    "down":       "Key.down",
    "left":       "Key.left",
    "right":      "Key.right",
    "home":       "Key.home",
    "end":        "Key.end",
    "pageup":     "Key.page_up",
    "pagedown":   "Key.page_down",
    "f1":  "Key.f1",  "f2":  "Key.f2",  "f3":  "Key.f3",  "f4":  "Key.f4",
    "f5":  "Key.f5",  "f6":  "Key.f6",  "f7":  "Key.f7",  "f8":  "Key.f8",
    "f9":  "Key.f9",  "f10": "Key.f10", "f11": "Key.f11", "f12": "Key.f12",
}

# macOS: backend maps "delete" → keycode 51 (Backspace).
# Windows: VK_DELETE is forward-delete → pynput emits Key.delete.
_KEYS_MACOS: dict[str, str] = dict(_KEYS_COMMON)
_KEYS_MACOS["delete"] = "Key.backspace"
_KEYS_MACOS["forward_delete"] = "Key.delete"

_KEYS_WINDOWS: dict[str, str] = dict(_KEYS_COMMON)
_KEYS_WINDOWS["delete"] = "Key.delete"

_CHAR_KEYS = ["a", "z", "1", "9", "/", ".", "-", "="]

# Modifier combos. Each row:
#   (key_press args, required_reprs)
# - required_reprs: pynput reprs that MUST appear in the event stream.
#   Both macOS (CGEvent) and Windows (SendInput) emit separate modifier
#   events — pynput sees Key.cmd / Key.ctrl / Key.alt / Key.shift.
#   Note: CGEvent/SendInput inject raw keycodes, so Shift doesn't upper-case
#   the char at the HID layer — 'z' + Key.shift is correct (apps do the
#   Shift→Z translation).
_MOD_COMBOS_MACOS: list[tuple[tuple[str, ...], list[str]]] = [
    (("cmd", "a"),           ["Key.cmd", "a"]),
    (("cmd", "shift", "z"),  ["Key.cmd", "Key.shift", "z"]),
    (("ctrl", "b"),          ["Key.ctrl", "b"]),
    (("option", "e"),        ["Key.alt"]),
    (("cmd", "return"),      ["Key.cmd", "Key.enter"]),
    (("shift", "tab"),       ["Key.shift", "Key.tab"]),
]

_MOD_COMBOS_WINDOWS: list[tuple[tuple[str, ...], list[str]]] = [
    (("ctrl", "a"),           ["Key.ctrl_l", "\x01"]),
    (("ctrl", "shift", "z"),  ["Key.ctrl_l", "Key.shift", "\x1a"]),
    (("alt", "f"),            ["Key.alt_l", "f"]),
    (("ctrl", "return"),      ["Key.ctrl_l", "Key.enter"]),
    (("shift", "tab"),        ["Key.shift", "Key.tab"]),
]


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_keys(platform, listener: InputListener, report: Report, is_macos: bool) -> None:
    print("\n== key_press \u2014 special keys ==")
    key_map = _KEYS_MACOS if is_macos else _KEYS_WINDOWS
    for key_name, expected in key_map.items():
        listener.clear()
        try:
            platform.key_press(key_name)
        except NotImplementedError as e:
            report.fail(f"key_press({key_name!r})", f"not implemented: {e}")
            continue
        reprs = [e.key_repr for e in listener.read_keys()]
        if expected in reprs:
            report.ok(f"key_press({key_name!r}) \u2192 {expected}")
        else:
            report.fail(f"key_press({key_name!r})", f"expected {expected!r}, got {reprs}")

    print("\n== key_press \u2014 printable characters ==")
    for ch in _CHAR_KEYS:
        listener.clear()
        try:
            platform.key_press(ch)
        except NotImplementedError as e:
            report.fail(f"key_press({ch!r})", f"not implemented: {e}")
            continue
        reprs = [e.key_repr for e in listener.read_keys()]
        if ch in reprs:
            report.ok(f"key_press({ch!r})")
        else:
            report.fail(f"key_press({ch!r})", f"expected {ch!r}, got {reprs}")

    print("\n== key_press \u2014 modifier combos ==")
    combos = _MOD_COMBOS_MACOS if is_macos else _MOD_COMBOS_WINDOWS
    for keys, required_reprs in combos:
        name = f"key_press({'+'.join(keys)})"
        listener.clear()
        try:
            platform.key_press(*keys)
        except NotImplementedError as e:
            report.fail(name, f"not implemented: {e}")
            continue
        reprs = [e.key_repr for e in listener.read_keys(wait=0.45)]
        missing = [r for r in required_reprs if r not in reprs]
        if missing:
            report.fail(name, f"missing {missing} in {reprs}")
            continue
        report.ok(f"{name} \u2192 {reprs}")


def test_type_text(platform, listener: InputListener, report: Report) -> None:
    print("\n== type_text (clipboard + paste hot-key) ==")
    # Both macOS (CGEvent) and Windows (SendInput) now emit separate modifier
    # events — pynput sees both the modifier and the trigger key.
    paste_mod = "Key.cmd" if sys.platform == "darwin" else "Key.ctrl_l"
    paste_key = "v" if sys.platform == "darwin" else "\x16"

    samples = [
        "hello",
        "Mixed CASE 123",
        "unicode: caf\u00e9 r\u00e9sum\u00e9 \u00fcnlaut",
        "CJK: \u4f60\u597d\uff0c\u4e16\u754c",
        "symbols: !@#$%^&*()_+-=[]{}|;:,.<>?/",
        'quotes: "double" and \'single\'',
    ]
    for sample in samples:
        name = f"type_text({sample!r})"
        listener.clear()
        try:
            platform.type_text(sample)
        except NotImplementedError as e:
            report.fail(name, f"not implemented: {e}")
            continue
        time.sleep(0.4)

        # (1) clipboard carries the exact sample.
        try:
            clip = platform.get_clipboard()
        except Exception as e:
            report.fail(name, f"get_clipboard failed: {e}")
            continue
        if clip.kind != "text":
            report.fail(name, f"clipboard kind={clip.kind} (expected text)")
            continue
        if clip.text != sample:
            report.fail(name, f"clipboard mismatch: expected {sample!r}, got {clip.text!r}")
            continue

        # (2) paste hot-key fired.
        reprs = [e.key_repr for e in listener.read_keys(wait=0.0)]
        if paste_mod in reprs and paste_key in reprs:
            report.ok(name)
        else:
            report.fail(
                name,
                f"paste hot-key ({paste_mod}+{paste_key}) not observed; keys={reprs}",
            )


def test_mouse(platform, listener: InputListener, report: Report) -> None:
    print("\n== mouse ==")

    try:
        start_pos = platform.get_cursor_position()
    except Exception:
        start_pos = (100, 100)

    try:
        displays = platform.get_displays()
    except Exception:
        displays = []
    if displays:
        d = displays[0]
        cx = d.origin_x + d.width // 2
        cy = d.origin_y + d.height // 2
    else:
        cx, cy = 400, 400

    # move_cursor — verified via get_cursor_position (exact OS query).
    target = (cx - 80, cy - 60)
    listener.clear()
    try:
        platform.move_cursor(*target)
    except NotImplementedError as e:
        report.fail("move_cursor", f"not implemented: {e}")
    else:
        time.sleep(0.1)
        try:
            actual = platform.get_cursor_position()
        except Exception as e:
            report.fail("move_cursor", f"get_cursor_position failed: {e}")
        else:
            dx = abs(actual[0] - target[0])
            dy = abs(actual[1] - target[1])
            if dx <= 3 and dy <= 3:
                report.ok(f"move_cursor \u2192 {actual}")
            else:
                report.fail(
                    "move_cursor",
                    f"expected ~{target}, got {actual}",
                )

    # click(left)
    listener.clear()
    platform.click(cx, cy, "left")
    clicks = [e for e in listener.read_mouse() if isinstance(e, MouseClickEvent)]
    lefts = [c for c in clicks if c.button == "left" and c.pressed]
    if lefts:
        c = lefts[0]
        dx = abs(c.x - cx)
        dy = abs(c.y - cy)
        if dx <= 3 and dy <= 3:
            report.ok(f"click(left) at ({c.x},{c.y})")
        else:
            report.fail("click(left)", f"coord drift: sent ({cx},{cy}), saw ({c.x},{c.y})")
    else:
        report.fail("click(left)", f"no left-press event (got {clicks})")

    # click(right)
    listener.clear()
    platform.click(cx, cy, "right")
    clicks = [e for e in listener.read_mouse() if isinstance(e, MouseClickEvent)]
    rights = [c for c in clicks if c.button == "right" and c.pressed]
    if rights:
        report.ok(f"click(right) at ({rights[0].x},{rights[0].y})")
    else:
        report.fail("click(right)", f"no right-press event (got {clicks})")

    # double_click — expect two left-press events
    listener.clear()
    platform.click(cx, cy, "left", click_count=2)
    clicks = [
        e for e in listener.read_mouse(wait=0.5)
        if isinstance(e, MouseClickEvent) and e.button == "left" and e.pressed
    ]
    if len(clicks) >= 2:
        report.ok(f"double_click ({len(clicks)} left-presses)")
    else:
        report.fail("double_click", f"expected \u22652 left presses, got {len(clicks)}")

    # scroll — 4 directions
    for direction, check in (
        ("down",  lambda ev: ev.dy < 0),
        ("up",    lambda ev: ev.dy > 0),
        ("right", lambda ev: ev.dx != 0),
        ("left",  lambda ev: ev.dx != 0),
    ):
        listener.clear()
        try:
            platform.scroll(cx, cy, direction=direction, amount=3)
        except NotImplementedError as e:
            report.fail(f"scroll({direction})", f"not implemented: {e}")
            continue
        scrolls = [e for e in listener.read_mouse(wait=0.4) if isinstance(e, MouseScrollEvent)]
        if not scrolls:
            report.fail(f"scroll({direction})", "no scroll event observed")
            continue
        if any(check(s) for s in scrolls):
            s = scrolls[0]
            report.ok(f"scroll({direction}) dx={s.dx} dy={s.dy}")
        else:
            report.fail(
                f"scroll({direction})",
                f"wrong direction, got {[(s.dx, s.dy) for s in scrolls]}",
            )

    # Restore cursor
    try:
        platform.move_cursor(*start_pos)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    only = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--only" and i + 2 <= len(sys.argv) - 1:
            only = sys.argv[i + 2]

    try:
        import pynput  # noqa: F401
    except ImportError:
        print(
            "ERROR: pynput is required (listed in pyproject.toml). Run: uv sync",
            file=sys.stderr,
        )
        return 2

    from protean.platform import get_platform

    platform = get_platform()
    is_macos = platform.name == "macos"
    is_windows = platform.name == "windows"
    if not (is_macos or is_windows):
        print(f"Platform {platform.name!r} has no input-simulation backend \u2014 skipping.")
        return 0

    print(f"Detected platform: {platform.name}")
    print(f"Python:            {sys.executable}")
    print(
        "NOTE: this test generates real synthetic input. Do not touch the\n"
        "      keyboard/mouse while it runs. Clipboard contents WILL be\n"
        "      overwritten during the type_text section."
    )
    print("Starting global input listener (pynput)\u2026")

    listener = InputListener()
    listener.start()
    report = Report()

    try:
        if only in (None, "keys"):
            test_keys(platform, listener, report, is_macos)
        if only in (None, "mouse"):
            test_mouse(platform, listener, report)
        if only in (None, "text"):
            test_type_text(platform, listener, report)
    finally:
        listener.stop()

    print("\n" + "=" * 60)
    print(f"Passed: {len(report.passed)}")
    print(f"Failed: {len(report.failed)}")
    for name, reason in report.failed:
        print(f"  FAIL  {name}: {reason}")
    return 0 if not report.failed else 1


if __name__ == "__main__":
    sys.exit(main())

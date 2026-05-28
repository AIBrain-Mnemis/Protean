"""Event compaction — merge consecutive low-level events into semantic units.

Rules:
  - Consecutive key_down/key_up with same window → merge into TEXT_INPUT "typed: ..."
  - Consecutive scroll events (< 500ms gap, same window) → merge into one scroll summary
  - Consecutive mouse_move → already throttled by monitor; drop for evidence
  - Consecutive mouse_drag → merge into one drag (start → end)
  - key_combo events are always kept individually (they are salient)
  - mouse_click, app_switch are always kept individually
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Event types that are always salient (never merged away)
SALIENT_TYPES = {
    "mouse_click",
    "mouse_double_click",
    "key_combo",
    "app_switch",
    "text_input",  # already compacted by input_monitor
}

# Types to merge
TYPING_TYPES = {"key_press", "key_release"}
SCROLL_TYPES = {"mouse_scroll"}
MOVE_TYPES = {"mouse_move"}
DRAG_TYPES = {"mouse_drag_start", "mouse_drag_end"}


def _psr_target(meta: dict[str, Any] | None) -> str:
    """Render the PSR-style target phrase '"<label> (<role>)"' or '' if no element."""
    if not meta:
        return ""
    el_label = meta.get("element_label", "")
    el_role = meta.get("element_role", "")
    if not el_label:
        return ""
    if el_role:
        return f'"{el_label} ({el_role})"'
    return f'"{el_label}"'


def _psr_in(title: str) -> str:
    """Render the trailing '(in "<title>")' phrase, empty if no title."""
    return f' (in "{title}")' if title else ""


@dataclass
class CompactedEvent:
    """A semantically compacted event for LLM consumption."""

    timestamp: float
    event_type: str  # "click", "type", "scroll", "hotkey", "app_switch", "drag"
    description: str  # human-readable: "Clicked 'Submit' button in Chrome"
    app: str
    window_title: str
    x: int | None = None
    y: int | None = None
    # For "type" events
    text: str = ""
    # For "hotkey" / key_combo
    keys: str = ""  # e.g. "cmd+c"
    # For scroll
    scroll_total: int = 0
    scroll_h_total: int = 0
    end_timestamp: float = 0
    # Raw event indices (for frame extraction timing)
    source_indices: list[int] | None = None
    # Enrichment metadata from input_monitor (element info, clipboard, etc.)
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "timestamp": round(self.timestamp, 2),
            "type": self.event_type,
            "description": self.description,
            "app": self.app,
        }
        if self.x is not None:
            d["x"] = self.x
            d["y"] = self.y
        if self.text:
            d["text"] = self.text
        if self.keys:
            d["keys"] = self.keys
        if self.scroll_total:
            d["scroll_total"] = self.scroll_total
        if self.scroll_h_total:
            d["scroll_h_total"] = self.scroll_h_total
        if self.end_timestamp:
            d["end_timestamp"] = round(self.end_timestamp, 2)
        if self.metadata:
            d["metadata"] = self.metadata
        return d


def compact_events(raw_events: list[dict[str, Any]]) -> list[CompactedEvent]:
    """Compact a list of raw events from events.json into semantic units."""
    # Drop the last event if it's a key_combo (likely the stop hotkey)
    if raw_events and raw_events[-1].get("event_type") == "key_combo":
        raw_events = raw_events[:-1]

    result: list[CompactedEvent] = []
    i = 0
    n = len(raw_events)

    while i < n:
        ev = raw_events[i]
        et = ev.get("event_type", "")
        win = ev.get("window", {})
        app = win.get("process_name", "")
        title = win.get("window_title", "")
        ts = ev.get("timestamp", 0)

        # ── Already-compacted text input — merge consecutive ones ──
        if et == "text_input":
            texts = [ev.get("text", "")]
            indices = [i]
            last_app = app
            last_title = title
            first_ts = ts
            i += 1
            # Merge consecutive text_input events in same window.
            # Also absorb intervening key_press(space/caps_lock) and
            # key_combo(shift+single_char) that are part of the same
            # typing sequence.
            while i < n:
                e2 = raw_events[i]
                e2t = e2.get("event_type", "")
                w2 = e2.get("window", {})
                a2 = w2.get("process_name", "")

                if e2t == "text_input":
                    if a2 and a2 != last_app:
                        break
                    texts.append(e2.get("text", ""))
                    indices.append(i)
                    last_app = a2 or last_app
                    last_title = w2.get("window_title", last_title)
                    i += 1
                    continue

                # Absorb space / caps_lock between text_input chunks
                if e2t == "key_press":
                    key2 = e2.get("key_char", "") or e2.get("key", "")
                    if key2 == "space":
                        texts.append(" ")
                        indices.append(i)
                        i += 1
                        continue
                    if key2 == "caps_lock":
                        # Just skip it — no textual contribution
                        indices.append(i)
                        i += 1
                        continue

                # Absorb shift+single_char as uppercase letter
                if e2t == "key_combo":
                    mods2 = e2.get("modifiers", [])
                    key2 = e2.get("key", "")
                    if mods2 == ["shift"] and len(key2) == 1:
                        texts.append(key2)  # already uppercase
                        indices.append(i)
                        i += 1
                        continue

                # Absorb key_release silently
                if e2t == "key_release":
                    indices.append(i)
                    i += 1
                    continue

                break

            combined = "".join(texts)
            meta = ev.get("metadata") or {}
            target = _psr_target(meta)
            if target:
                desc = (
                    f"User typed [{_truncate(combined, 60)}] "
                    f"into {target}{_psr_in(last_title)}"
                )
            else:
                desc = f'Typed "{_truncate(combined, 60)}" in {last_app}'
            result.append(
                CompactedEvent(
                    timestamp=first_ts,
                    event_type="type",
                    description=desc,
                    app=last_app,
                    window_title=last_title,
                    text=combined,
                    metadata=meta or None,
                    source_indices=indices,
                )
            )
            continue

        # ── Mouse click ──
        if et in ("mouse_click", "mouse_double_click"):
            # Lookahead: skip this single click if it's the first half
            # of a double-click or drag.  Search up to 3 events ahead
            # within 0.5s to handle intervening events (APP_SWITCH, etc.).
            if et == "mouse_click":
                skip = False
                for j in range(i + 1, min(i + 4, n)):
                    nj = raw_events[j]
                    njt = nj.get("event_type", "")
                    nj_ts = nj.get("timestamp", 0)
                    if nj_ts - ts > 0.5:
                        break
                    nx, ny = nj.get("x", -1), nj.get("y", -1)
                    nearby = (
                        abs(ev.get("x", 0) - nx) <= 10
                        and abs(ev.get("y", 0) - ny) <= 10
                    )
                    if njt in ("mouse_drag_start", "mouse_double_click") and nearby:
                        click_meta = ev.get("metadata")
                        if click_meta:
                            nj_meta = nj.get("metadata") or {}
                            nj_meta.update(click_meta)
                            nj["metadata"] = nj_meta
                        skip = True
                        break
                if skip:
                    i += 1
                    continue
            click_type = "Double-clicked" if et == "mouse_double_click" else "Clicked"
            x, y = ev.get("x", 0), ev.get("y", 0)
            meta = ev.get("metadata") or {}
            target = _psr_target(meta)
            button = ev.get("button", "left")
            if et == "mouse_double_click":
                action_en = "Left double-clicked"
            elif button == "right":
                action_en = "Right-clicked"
            elif button == "middle":
                action_en = "Middle-clicked"
            else:
                action_en = "Left-clicked"
            if target:
                desc = f"User {action_en} on {target}{_psr_in(title)}"
            else:
                el_label = meta.get("element_label", "")
                el_role = meta.get("element_role", "")
                if el_label:
                    legacy_target = f"'{el_label}'"
                    if el_role:
                        legacy_target += f" ({el_role})"
                    desc = f"{click_type} {legacy_target} at ({x}, {y}) in {app}"
                else:
                    desc = f"{click_type} at ({x}, {y}) in {app} — {title}"
            result.append(
                CompactedEvent(
                    timestamp=ts,
                    event_type="click",
                    description=desc,
                    app=app,
                    window_title=title,
                    x=x,
                    y=y,
                    metadata=meta or None,
                    source_indices=[i],
                )
            )
            i += 1
            continue

        # ── Key combo (hotkey) ──
        if et == "key_combo":
            mods = ev.get("modifiers", [])
            key = ev.get("key", "")
            combo = "+".join(mods + [key])
            meta = ev.get("metadata") or {}
            cb_kind = meta.get("clipboard_kind", "")
            target = _psr_target(meta)
            if cb_kind == "text":
                cb_text = meta.get("clipboard_text", "")
                cb_suffix = f' — clipboard: "{_truncate(cb_text, 40)}"'
            elif cb_kind == "files":
                cb_files = meta.get("clipboard_files", [])
                cb_suffix = f" — clipboard: {len(cb_files)} file(s)"
            elif cb_kind == "image":
                cb_suffix = " — clipboard: image"
            else:
                cb_suffix = ""
            if target:
                desc = f"User pressed {combo} on {target}{_psr_in(title)}{cb_suffix}"
            else:
                desc = f"Pressed {combo} in {app} — {title}{cb_suffix}"
            result.append(
                CompactedEvent(
                    timestamp=ts,
                    event_type="hotkey",
                    description=desc,
                    app=app,
                    window_title=title,
                    keys=combo,
                    metadata=meta or None,
                    source_indices=[i],
                )
            )
            i += 1
            continue

        # ── App switch ──
        if et == "app_switch":
            result.append(
                CompactedEvent(
                    timestamp=ts,
                    event_type="app_switch",
                    description=f"Switched to {app} — {title}",
                    app=app,
                    window_title=title,
                    source_indices=[i],
                )
            )
            i += 1
            continue

        # ── Consecutive key_press → merge into typing ──
        if et == "key_press":
            chars: list[str] = []
            indices: list[int] = []
            first_ts = ts
            last_app, last_title = app, title

            while i < n:
                e2 = raw_events[i]
                e2t = e2.get("event_type", "")
                if e2t not in TYPING_TYPES:
                    break
                # Skip key_release entirely
                if e2t == "key_release":
                    indices.append(i)
                    i += 1
                    continue
                # Check window change
                w2 = e2.get("window", {})
                a2 = w2.get("process_name", "")
                if a2 and a2 != last_app and chars:
                    break  # different app → flush
                char = e2.get("key_char", "") or e2.get("key", "")
                if len(char) == 1:
                    chars.append(char)
                else:
                    # Non-printable key like "enter", "backspace"
                    if chars:
                        break  # flush accumulated text first
                    # Emit as individual key event
                    e2_meta = e2.get("metadata") or {}
                    e2_target = _psr_target(e2_meta)
                    e2_title = w2.get("window_title", last_title)
                    if e2_target:
                        e2_desc = f"User pressed {char} on {e2_target}{_psr_in(e2_title)}"
                    else:
                        e2_desc = f"Pressed {char} in {a2 or last_app}"
                    result.append(
                        CompactedEvent(
                            timestamp=e2.get("timestamp", 0),
                            event_type="key",
                            description=e2_desc,
                            app=a2 or last_app,
                            window_title=e2_title,
                            keys=char,
                            metadata=e2_meta or None,
                            source_indices=[i],
                        )
                    )
                    indices.append(i)
                    i += 1
                    break
                indices.append(i)
                last_app = a2 or last_app
                last_title = w2.get("window_title", last_title)
                i += 1

            if chars:
                text = "".join(chars)
                # Find first key_press event in indices to read its metadata
                merge_meta: dict[str, Any] = {}
                for idx in indices:
                    em = raw_events[idx].get("metadata") or {}
                    if em.get("element_label"):
                        merge_meta = em
                        break
                target = _psr_target(merge_meta)
                if target:
                    desc = (
                        f"User typed [{_truncate(text, 60)}] "
                        f"into {target}{_psr_in(last_title)}"
                    )
                else:
                    desc = f'Typed "{_truncate(text, 60)}" in {last_app}'
                result.append(
                    CompactedEvent(
                        timestamp=first_ts,
                        event_type="type",
                        description=desc,
                        app=last_app,
                        window_title=last_title,
                        text=text,
                        metadata=merge_meta or None,
                        source_indices=indices,
                    )
                )
            continue

        # ── Consecutive scroll → merge ──
        if et == "mouse_scroll":
            total_dx = 0
            total_dy = 0
            indices = []
            first_ts = ts
            last_ts = ts

            while i < n:
                e2 = raw_events[i]
                if e2.get("event_type") != "mouse_scroll":
                    break
                gap = e2.get("timestamp", 0) - last_ts
                if gap > 0.5 and indices:
                    break  # gap too big
                total_dx += e2.get("scroll_dx", 0)
                total_dy += e2.get("scroll_dy", 0)
                last_ts = e2.get("timestamp", 0)
                indices.append(i)
                i += 1

            parts = []
            en_parts = []
            if total_dy < 0:
                parts.append("down")
                en_parts.append("down")
            elif total_dy > 0:
                parts.append("up")
                en_parts.append("up")
            if total_dx > 0:
                parts.append("right")
                en_parts.append("right")
            elif total_dx < 0:
                parts.append("left")
                en_parts.append("left")
            direction = "+".join(parts) if parts else ""
            en_direction = "+".join(en_parts) if en_parts else ""
            ticks = abs(total_dy) + abs(total_dx)
            x, y = raw_events[indices[0]].get("x", 0), raw_events[indices[0]].get("y", 0)
            scroll_meta = raw_events[indices[0]].get("metadata") or {}
            target = _psr_target(scroll_meta)
            if target:
                desc = (
                    f"User scrolled mouse wheel {en_direction} on {target}"
                    f"{_psr_in(title)}"
                )
            else:
                desc = f"Scrolled {direction} ({ticks} ticks) in {app} — {title}"
            result.append(
                CompactedEvent(
                    timestamp=first_ts,
                    event_type="scroll",
                    description=desc,
                    app=app,
                    window_title=title,
                    x=x,
                    y=y,
                    scroll_total=total_dy,
                    scroll_h_total=total_dx,
                    metadata=scroll_meta or None,
                    source_indices=indices,
                )
            )
            continue

        # ── Skip mouse_move, screenshot, key_release ──
        if et in ("mouse_move", "screenshot", "key_release"):
            i += 1
            continue

        # ── Drag ──
        if et == "mouse_drag_start":
            start_x, start_y = ev.get("x", 0), ev.get("y", 0)
            meta = ev.get("metadata") or {}
            end_meta: dict[str, Any] = {}
            indices = [i]
            i += 1
            end_x, end_y = start_x, start_y
            end_ts = ts
            while i < n:
                e2 = raw_events[i]
                if e2.get("event_type") == "mouse_drag_end":
                    end_x, end_y = e2.get("x", 0), e2.get("y", 0)
                    end_ts = e2.get("timestamp", ts)
                    end_meta = e2.get("metadata") or {}
                    indices.append(i)
                    i += 1
                    break
                indices.append(i)
                i += 1
            start_target = _psr_target(meta)
            end_target = _psr_target(end_meta)
            if start_target or end_target:
                if start_target and end_target and start_target != end_target:
                    desc = (
                        f"User dragged from {start_target} to {end_target}"
                        f"{_psr_in(title)}"
                    )
                else:
                    pick = start_target or end_target
                    desc = (
                        f"User dragged {pick} from ({start_x},{start_y}) to "
                        f"({end_x},{end_y}){_psr_in(title)}"
                    )
            else:
                el_label = meta.get("element_label", "")
                el_role = meta.get("element_role", "")
                if el_label:
                    legacy_target = f"'{el_label}'"
                    if el_role:
                        legacy_target += f" ({el_role})"
                    desc = (
                        f"Dragged {legacy_target} from ({start_x},{start_y}) "
                        f"to ({end_x},{end_y}) in {app}"
                    )
                else:
                    desc = (
                        f"Dragged from ({start_x},{start_y}) to "
                        f"({end_x},{end_y}) in {app}"
                    )
            result.append(
                CompactedEvent(
                    timestamp=ts,
                    event_type="drag",
                    description=desc,
                    app=app,
                    window_title=title,
                    x=end_x,
                    y=end_y,
                    end_timestamp=end_ts,
                    metadata=meta or None,
                    source_indices=indices,
                )
            )
            continue

        # ── Fallback: skip unknown ──
        i += 1

    return result


def _truncate(text: str, maxlen: int) -> str:
    if len(text) <= maxlen:
        return text
    return text[: maxlen - 3] + "..."

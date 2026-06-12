"""Evidence pack builder — prepare recording data for LLM analysis.

  1. Compact raw events into semantic units (typing, scroll, etc.)
  2. For each salient event, extract a PAIRED frame from the video:
     - Overview: low-res full screenshot (spatial context)
     - Detail: high-res crop around the event coordinates (element detail)
  3. Format as interleaved event+image pairs for the LLM

This ensures the LLM sees EXACTLY what was clicked/typed and WHERE.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from protean.analyzer.compaction import CompactedEvent, compact_events
from protean.analyzer.frames import FramePair, extract_frame_pair
from protean.recorder.audio import transcribe

DEFAULT_MAX_CONTEXT = 200_000        # tokens — Anthropic Sonnet/Opus default
RESPONSE_TOKEN_RESERVE = 16_000       # leave room for the structured Skill output
SYSTEM_TOKEN_RESERVE = 8_000          # RECORDING_SYSTEM_PROMPT + toolkit prompt (~7.3k tok)
TOKENS_PER_CHAR = 1 / 3               # CJK-conservative (Chinese ≈ 1 tok/char, ASCII ≈ 0.25)
TOKENS_PER_IMAGE = 1500               # conservative across Anthropic / OpenAI vision
HARD_IMAGE_CAP = 100                  # provider single-request limit + attention safety


def max_context() -> int:
    raw = os.getenv("PROTEAN_MAX_CONTEXT")
    if not raw:
        return DEFAULT_MAX_CONTEXT
    try:
        return max(20_000, int(raw))
    except ValueError:
        return DEFAULT_MAX_CONTEXT


def explicit_image_budget() -> int | None:
    """Explicit override that bypasses context-derivation AND HARD_IMAGE_CAP.

    Set ``PROTEAN_GENERATE_IMAGE_BUDGET`` only when you intentionally want to
    take control of image count (e.g. for Gemini 1M with a known-safe value).
    """
    raw = os.getenv("PROTEAN_GENERATE_IMAGE_BUDGET")
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def derive_image_budget(estimated_text_chars: int, max_context: int) -> int:
    """Compute how many images fit given estimated text and context window."""
    text_tokens = int(estimated_text_chars * TOKENS_PER_CHAR)
    overhead = SYSTEM_TOKEN_RESERVE + RESPONSE_TOKEN_RESERVE
    available = max_context - overhead - text_tokens
    if available <= 0:
        return 0
    return min(HARD_IMAGE_CAP, available // TOKENS_PER_IMAGE)


def _probe_video_duration(video_path: Path) -> float | None:
    """Get video duration in seconds using ffprobe."""
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _detect_scene_changes(video_path: Path, threshold: float = 0.12) -> list[float]:
    """Detect scene change timestamps using ffmpeg scene filter.

    Returns list of timestamps (seconds) where significant visual changes occur.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                str(video_path),
                "-vf",
                f"select='gt(scene,{threshold})',showinfo",
                "-vsync",
                "vfr",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        # Parse timestamps from showinfo output
        timestamps: list[float] = []
        for line in result.stderr.split("\n"):
            if "pts_time:" in line:
                for part in line.split():
                    if part.startswith("pts_time:"):
                        ts = float(part.split(":")[1])
                        # Minimum 0.9s gap between scenes
                        if not timestamps or ts - timestamps[-1] >= 0.9:
                            timestamps.append(ts)
        return timestamps
    except Exception:
        return []


def _extract_audio(video_path: Path, output_path: Path) -> bool:
    """Extract audio from video to .m4a using ffmpeg. Returns True if audio exists."""
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-vn",  # no video
                "-acodec",
                "copy",  # copy audio stream as-is
                str(output_path),
            ],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        return False


def _collect_utterances(raw_events: list[dict]) -> list[dict]:
    """Collect speech events written by the recorder (VAD + ASR path).

    Returns a list of {"start": float, "end": float, "text": str} dicts in the
    order they appear in raw_events. Entries without a numeric ``timestamp`` get
    ``start = end = 0.0`` so they still surface in the timeline (sorted to the
    very front) rather than being silently dropped.
    """
    items: list[dict] = []
    for e in raw_events:
        if (e.get("event_type") or e.get("type")) != "speech":
            continue
        text = (e.get("transcript") or "").strip()
        if not text:
            continue
        ts = e.get("timestamp")
        start = float(ts) if isinstance(ts, (int, float)) else 0.0
        dur = e.get("audio_duration")
        end = start + (float(dur) if isinstance(dur, (int, float)) else 0.0)
        items.append({"start": start, "end": end, "text": text})
    return items


@dataclass
class EvidencePack:
    """Structured evidence prepared for LLM analysis."""

    recording_dir: Path | None
    duration: float
    total_raw_events: int
    compacted_events: list[CompactedEvent]
    frame_pairs: list[FramePair]
    app_summary: dict[str, int]  # app_name → event_count
    event_type_summary: dict[str, int]  # event_type → count
    utterances: list[dict] = field(default_factory=list)  # {start,end,text}

    def format_for_llm(
        self,
        task_description: str = "",
        include_images: bool = True,
    ) -> list[dict]:
        """Format this evidence pack into a unified chronological timeline.

        The timeline interleaves three kinds of entries by recording timestamp:
          * action with screenshots (overview + optional detail)
          * action without screenshots (text-only — image budget exceeded)
          * voice utterance (from the recorder's VAD+ASR speech events)

        Every compacted event and every utterance appears exactly once, in
        time order with a running ``[N]`` index. Image attachment is selected
        by ``_select_frames_for_images`` within ``include_images and budget``.
        """
        import json

        apps_used = ", ".join(
            f"{app} ({count})"
            for app, count in sorted(self.app_summary.items(), key=lambda x: -x[1])
        )
        event_breakdown = ", ".join(
            f"{et}: {count}"
            for et, count in sorted(
                self.event_type_summary.items(), key=lambda x: -x[1]
            )
        )

        lean = os.getenv("PROTEAN_GENERATE_TEXT_LEAN") == "1"

        # Map each compacted event to its frame_pair by identity ONLY.
        # Timestamp fallback is unsafe in the v2 path because every event
        # in legacy recordings can share timestamp=0 — one fallback pair
        # would then be attached to many unrelated actions.
        pair_by_event_id: dict[int, tuple[int, FramePair]] = {
            id(p.event): (i, p) for i, p in enumerate(self.frame_pairs)
        }

        action_entries: list[dict] = []
        for e in self.compacted_events:
            hit = pair_by_event_id.get(id(e))
            action_entries.append(
                {
                    "kind": "action",
                    "ts": e.timestamp,
                    "event": e,
                    "frame_index": hit[0] if hit else None,
                    "pair": hit[1] if hit else None,
                }
            )

        voice_entries: list[dict] = [
            {"kind": "voice", "ts": u["start"], "end": u["end"], "text": u["text"]}
            for u in self.utterances
        ]

        # Stable sort: voice before action at identical timestamp.
        entries = sorted(
            action_entries + voice_entries,
            key=lambda e: (e["ts"], 0 if e["kind"] == "voice" else 1),
        )

        # ── Pre-pass: format every text fragment so we can size the budget ──
        def _format_voice(n: int, entry: dict) -> str:
            ts = entry["ts"]
            end = entry["end"]
            if lean:
                ts_label = f"{int(ts)}s" if end <= ts else f"{int(ts)}-{int(end)}s"
            else:
                ts_label = f"{ts:.1f}s" if end <= ts else f"{ts:.1f}–{end:.1f}s"
            text = entry["text"].replace("\n", " ").strip()
            return f"### [{n}] 🎙 voice @ {ts_label}\n\"{text}\"\n"

        def _format_action_body(ev: CompactedEvent) -> tuple[str, str]:
            if ev.event_type == "scene_change":
                return "scene", ""
            payload = ev.to_dict()
            if lean:
                # Strip non-essential fields to shave tokens.
                payload = {
                    k: v for k, v in payload.items()
                    if k in {"event_type", "x", "y", "text", "keys", "app", "description"}
                       and v not in (None, "", [])
                }
                # Round coords to int.
                for k in ("x", "y"):
                    if isinstance(payload.get(k), float):
                        payload[k] = int(payload[k])
            body = (
                f"{ev.description}\n"
                f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```\n"
            )
            return "action", body

        def _format_action_header(n: int, entry: dict, img_tag: str) -> str:
            ev: CompactedEvent = entry["event"]
            tag, body = _format_action_body(ev)
            return f"### [{n}] {tag} @ {ev.timestamp:.1f}s [{img_tag}]\n{body}"

        # Compute text-char total assuming every action says "[overview]" so
        # the estimate is an upper bound on text size (we may downgrade some
        # to "[no screenshot attached]" later — that text is shorter, so
        # underestimating image overhead is impossible).
        estimated_text_chars = 0
        for n, entry in enumerate(entries, 1):
            if entry["kind"] == "voice":
                estimated_text_chars += len(_format_voice(n, entry))
            else:
                estimated_text_chars += len(
                    _format_action_header(n, entry, "overview + detail")
                )
                # account for "[overview_N]" + "[detail_N: ...]" labels
                estimated_text_chars += 64

        max_ctx = max_context()
        if not include_images:
            budget = 0
        else:
            explicit = explicit_image_budget()
            if explicit is not None:
                budget = explicit
            else:
                budget = derive_image_budget(estimated_text_chars, max_ctx)

        selected_overview, selected_detail = _select_frames_for_images(
            self.frame_pairs, self.utterances, budget
        )

        header_lines: list[str] = [
            "## Recording Summary",
            "",
            f"- Duration: {self.duration:.1f}s",
            f"- Raw events: {self.total_raw_events}",
            f"- Compacted actions: {len(self.compacted_events)}",
            f"- Voice utterances: {len(self.utterances)}",
            f"- Apps used: {apps_used or 'unknown'}",
            f"- Action breakdown: {event_breakdown or 'none'}",
        ]
        if task_description:
            header_lines.append(f"- User description: {task_description}")
        if include_images:
            est_text_tok = int(estimated_text_chars * TOKENS_PER_CHAR)
            header_lines.append(
                f"- Context window: {max_context} tok; "
                f"estimated text: ~{est_text_tok} tok; "
                f"image budget: {len(selected_overview)} overview "
                f"+ {len(selected_detail)} detail "
                f"(of {len(self.frame_pairs)} available frames)"
            )
        if lean:
            header_lines.append("- Mode: TEXT_LEAN (compacted JSON, integer coords)")
        header_lines.extend(["", "## Timeline", ""])

        content_parts: list[dict] = [
            {"type": "text", "text": "\n".join(header_lines)},
        ]

        for n, entry in enumerate(entries, 1):
            if entry["kind"] == "voice":
                content_parts.append(
                    {"type": "text", "text": _format_voice(n, entry)}
                )
                continue

            pair: FramePair | None = entry["pair"]
            frame_idx = entry["frame_index"]
            attach_overview = (
                include_images
                and pair is not None
                and frame_idx in selected_overview
            )
            attach_detail = (
                attach_overview
                and pair is not None
                and pair.detail_bytes is not None
                and frame_idx in selected_detail
            )
            if attach_overview:
                img_tag = "overview + detail" if attach_detail else "overview"
            else:
                img_tag = "no screenshot attached"

            content_parts.append(
                {"type": "text", "text": _format_action_header(n, entry, img_tag)}
            )

            if attach_overview and pair is not None:
                content_parts.append(
                    {"type": "text", "text": f"[overview_{n}]\n"}
                )
                content_parts.append(
                    {
                        "type": "image",
                        "data": pair.overview_bytes,
                        "mime": "image/jpeg",
                    }
                )
                if attach_detail:
                    content_parts.append(
                        {
                            "type": "text",
                            "text": f"[detail_{n}: {pair.detail_crop_info}]\n",
                        }
                    )
                    content_parts.append(
                        {
                            "type": "image",
                            "data": pair.detail_bytes,
                            "mime": "image/jpeg",
                        }
                    )

        content_parts.append({"type": "text", "text": "Now output the skill."})
        return content_parts


def _select_frames_for_images(
    frame_pairs: list[FramePair],
    utterances: list[dict],
    budget: int,
) -> tuple[set[int], set[int]]:
    """Pick which frame_pairs keep their images, within ``budget`` total images.

    Returns ``(overview_indices, detail_indices)`` — sets of indices into
    ``frame_pairs``. Each overview costs 1; each detail costs 1 more.

    Strategy:
      1. Score each pair (event-type weight, scene-change, voice overlap,
         endpoints, first-app appearance, detail availability).
      2. Pick top-scoring pairs to fill ``budget // 2`` overview slots.
      3. Reserve the other half for temporal coverage: split duration into
         buckets and ensure each bucket has at least one selected overview.
      4. Spend leftover budget on detail crops, prioritized by score.
    """
    if budget <= 0 or not frame_pairs:
        return set(), set()

    duration = max(p.event.timestamp for p in frame_pairs) if frame_pairs else 0.0

    def score(idx: int, pair: FramePair) -> float:
        ev = pair.event
        s = 1.0
        if ev.event_type in {"app_switch", "type", "hotkey"}:
            s += 2.0
        if ev.event_type == "scene_change":
            s += 1.0
        for u in utterances:
            if u["start"] - 0.5 <= ev.timestamp <= u["end"] + 1.0:
                s += 1.5
                break
        if idx == 0 or idx == len(frame_pairs) - 1:
            s += 1.0
        if pair.detail_bytes is not None:
            s += 0.5
        return s

    # First-appearance bonus per app
    seen_apps: set[str] = set()
    bonuses: dict[int, float] = {}
    for i, p in enumerate(frame_pairs):
        if p.event.app and p.event.app not in seen_apps:
            seen_apps.add(p.event.app)
            bonuses[i] = bonuses.get(i, 0.0) + 0.5
    scores = [(i, score(i, p) + bonuses.get(i, 0.0)) for i, p in enumerate(frame_pairs)]

    overview_budget = max(1, budget // 2) if budget >= 2 else budget
    half_top = max(1, overview_budget // 2)

    # Step 1: top-scoring half
    top_sorted = sorted(scores, key=lambda x: -x[1])
    selected: set[int] = {i for i, _ in top_sorted[:half_top]}

    # Step 2: temporal coverage — bucket the remaining half
    remaining_slots = overview_budget - len(selected)
    if remaining_slots > 0 and duration > 0:
        bucket_count = remaining_slots
        bucket_width = duration / bucket_count if bucket_count else duration
        for b in range(bucket_count):
            lo = b * bucket_width
            hi = lo + bucket_width if b < bucket_count - 1 else duration + 1e-6
            if any(lo <= frame_pairs[i].event.timestamp <= hi for i in selected):
                continue
            # Pick highest-scoring frame in this bucket not yet selected
            candidates = [
                (i, sc) for i, sc in scores
                if lo <= frame_pairs[i].event.timestamp <= hi and i not in selected
            ]
            if candidates:
                pick = max(candidates, key=lambda x: x[1])[0]
                selected.add(pick)
            if len(selected) >= overview_budget:
                break

    # Step 2b: if temporal pass left budget on the table (e.g. no usable
    # timestamps), fill with the next highest-scoring frames.
    if len(selected) < overview_budget:
        for i, _ in top_sorted:
            if i in selected:
                continue
            selected.add(i)
            if len(selected) >= overview_budget:
                break

    # Trim if temporal-fill overshot (shouldn't, but be safe)
    if len(selected) > overview_budget:
        kept_scores = sorted(((i, dict(scores)[i]) for i in selected), key=lambda x: -x[1])
        selected = {i for i, _ in kept_scores[:overview_budget]}

    # Step 3: spend remaining budget on detail crops
    detail_capacity = budget - len(selected)
    detail_candidates = sorted(
        (i for i in selected if frame_pairs[i].detail_bytes is not None),
        key=lambda i: -dict(scores)[i],
    )
    detail_selected: set[int] = set(detail_candidates[: max(0, detail_capacity)])

    # Step 4: any unspent capacity (e.g. no detail crops available) → more overviews.
    leftover = budget - len(selected) - len(detail_selected)
    if leftover > 0:
        for i, _ in top_sorted:
            if i in selected:
                continue
            selected.add(i)
            leftover -= 1
            if leftover <= 0:
                break

    return selected, detail_selected


def _build_v2_frame_pairs(
    compacted: list[CompactedEvent],
    raw_events: list[dict],
    recording_dir: Path,
) -> list[FramePair]:
    """v2: pull on-disk screenshots that were captured at event time.

    For each compacted event, look up its source raw events (via source_indices)
    and use the screenshot of the LAST source event that has one — typically
    representing the post-action state.
    """
    pairs: list[FramePair] = []
    for ce in compacted:
        if not ce.source_indices:
            continue
        chosen: dict | None = None
        for idx in reversed(ce.source_indices):
            if 0 <= idx < len(raw_events):
                shot = raw_events[idx].get("screenshot")
                if shot and shot.get("overview"):
                    chosen = shot
                    break
        if not chosen:
            continue

        overview_path = recording_dir / chosen["overview"]
        if not overview_path.exists():
            continue
        try:
            overview_bytes = overview_path.read_bytes()
        except Exception:
            continue

        detail_bytes: bytes | None = None
        detail_info = chosen.get("crop_info", "")
        detail_rel = chosen.get("detail")
        if detail_rel:
            detail_path = recording_dir / detail_rel
            if detail_path.exists():
                try:
                    detail_bytes = detail_path.read_bytes()
                except Exception:
                    detail_bytes = None

        pairs.append(
            FramePair(
                event=ce,
                overview_bytes=overview_bytes,
                overview_width=0,
                overview_height=0,
                detail_bytes=detail_bytes,
                detail_width=0,
                detail_height=0,
                detail_crop_info=detail_info,
            )
        )
    return pairs


def build_evidence_pack(recording_dir: Path) -> EvidencePack:
    """Build an evidence pack from a recording directory.

    Steps:
      1. Load events.json
      2. Compact raw events into semantic units
      3. Extract frame pairs (overview + detail) for salient events
    """
    events_file = recording_dir / "events.json"
    raw = json.loads(events_file.read_text(encoding="utf-8"))
    all_events: list[dict] = raw.get("events", [])
    duration = raw.get("duration", 0)

    # Step 1: Compact events
    compacted = compact_events(all_events)

    # Per-utterance speech events written by the recorder (VAD+ASR path)
    utterances = _collect_utterances(all_events)

    # ── If per-event screenshots exist on disk, use them (faster & more precise) ──
    screenshots_dir = recording_dir / "screenshots"
    has_screenshots = screenshots_dir.is_dir() and any(screenshots_dir.iterdir())
    if has_screenshots:
        frame_pairs = _build_v2_frame_pairs(compacted, all_events, recording_dir)

        app_summary: dict[str, int] = {}
        event_type_summary: dict[str, int] = {}
        for e in compacted:
            app_summary[e.app] = app_summary.get(e.app, 0) + 1
            event_type_summary[e.event_type] = event_type_summary.get(e.event_type, 0) + 1

        return EvidencePack(
            recording_dir=recording_dir,
            duration=duration,
            total_raw_events=len(all_events),
            compacted_events=compacted,
            frame_pairs=frame_pairs,
            app_summary=app_summary,
            event_type_summary=event_type_summary,
            utterances=utterances,
        )

    # Step 2: Identify frame extraction timestamps from two sources
    #   Mode A: Event-triggered (clicks, typing, hotkeys)
    #   Mode B: Scene-change (visual deltas detected by ffmpeg)

    video_path = recording_dir / "recording.mov"
    if not video_path.exists():
        raise FileNotFoundError(
            f"No recording.mov found in {recording_dir}. "
            f"Screen recording is required — re-run 'protean record'."
        )

    # Time offset: events may start before video
    video_duration = _probe_video_duration(video_path)
    time_offset = 0.0
    if video_duration and duration > 0:
        time_offset = max(0.0, duration - video_duration)

    display_info = raw.get("display", {})
    display_width = display_info.get("width", 0)
    display_origin = (display_info.get("origin_x", 0), display_info.get("origin_y", 0))
    display_scale_factor = display_info.get("scale_factor", 0)

    # Mode A: salient events
    salient = [
        e for e in compacted
        if e.event_type in ("click", "hotkey", "type", "drag", "app_switch", "key")
    ]

    # Mode B: scene changes
    scene_timestamps = _detect_scene_changes(video_path)

    # Build unified timeline: (timestamp_in_video, source, event_or_none)
    timeline: list[tuple[float, str, CompactedEvent | None]] = []

    for event in salient:
        ts = (
            event.end_timestamp
            if event.event_type == "drag" and event.end_timestamp
            else event.timestamp
        )
        video_ts = max(0.0, ts - time_offset)
        timeline.append((video_ts, "event", event))

    for ts in scene_timestamps:
        # Check this scene change isn't too close to an existing event frame
        too_close = any(abs(ts - t[0]) < 1.0 for t in timeline)
        if not too_close:
            timeline.append((ts, "scene", None))

    # Sort by time
    timeline.sort(key=lambda t: t[0])

    # Step 3: Extract frame pairs
    work_dir_obj = tempfile.TemporaryDirectory(prefix="protean-frames-")
    work_dir = Path(work_dir_obj.name)
    frame_pairs: list[FramePair] = []

    for idx, (video_ts, source, event) in enumerate(timeline):
        if event:
            adjusted = CompactedEvent(
                timestamp=video_ts,
                event_type=event.event_type,
                description=event.description,
                app=event.app,
                window_title=event.window_title,
                x=event.x,
                y=event.y,
                text=event.text,
                keys=event.keys,
                scroll_total=event.scroll_total,
                source_indices=event.source_indices,
            )
        else:
            # Scene change: no coordinates, no specific event
            adjusted = CompactedEvent(
                timestamp=video_ts,
                event_type="scene_change",
                description=f"Scene change at {video_ts + time_offset:.1f}s",
                app="",
                window_title="",
            )

        pair = extract_frame_pair(
            video_path, adjusted, work_dir, idx,
            display_width=display_width, display_origin=display_origin,
            scale_factor=display_scale_factor,
        )
        if pair:
            if event:
                pair.event = event  # restore recording timebase
            else:
                # Restore recording timebase for scene frames too
                pair.event.timestamp = video_ts + time_offset
            frame_pairs.append(pair)

    # Summaries
    app_summary: dict[str, int] = {}
    event_type_summary: dict[str, int] = {}
    for e in compacted:
        app_summary[e.app] = app_summary.get(e.app, 0) + 1
        event_type_summary[e.event_type] = event_type_summary.get(e.event_type, 0) + 1

    # Step 4: Transcribe audio if present (only when recorder didn't already
    # capture per-utterance speech events)
    if not utterances:
        audio_path = work_dir / "audio.m4a"
        if _extract_audio(video_path, audio_path):
            text = transcribe(audio_path)
            if text:
                utterances = [{"start": 0.0, "end": duration, "text": text}]

    # Clean up temporary frame files
    work_dir_obj.cleanup()

    return EvidencePack(
        recording_dir=recording_dir,
        duration=duration,
        total_raw_events=len(all_events),
        compacted_events=compacted,
        frame_pairs=frame_pairs,
        app_summary=app_summary,
        event_type_summary=event_type_summary,
        utterances=utterances,
    )

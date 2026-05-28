"""Frame extraction — extract overview + detail image pairs from video.

For each salient event with coordinates:
  1. Overview: extract full frame at event timestamp, scale to low res (e.g. 800px wide)
  2. Detail:   extract full frame at native resolution, crop a region centered on (x, y)

This gives the LLM both spatial context AND element-level detail.
"""

from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from protean.analyzer.compaction import CompactedEvent

OVERVIEW_MAX_WIDTH = 800  # low-res overview
DETAIL_CROP_SIZE = 400  # detail crop width/height at native resolution
DETAIL_MIN_SIZE = 200  # minimum crop size
JPEG_QUALITY = 75  # JPEG quality for LLM images (balance size vs clarity)


def event_xy_to_native(
    event_x: int,
    event_y: int,
    native_w: int,
    display_width: int = 0,
    display_origin: tuple[int, int] = (0, 0),
    scale_factor: float = 0,
) -> tuple[int, int]:
    """Convert global logical event coords to native-image pixel coords."""
    rel_x = event_x - display_origin[0]
    rel_y = event_y - display_origin[1]
    if scale_factor > 0:
        scale = scale_factor
    else:
        scale = native_w / display_width if display_width > 0 else 1.0
    return int(rel_x * scale), int(rel_y * scale)


@dataclass
class FramePair:
    """A paired overview + detail image for a single salient event."""

    event: CompactedEvent
    overview_bytes: bytes  # PNG, low-res full screenshot
    overview_width: int
    overview_height: int
    detail_bytes: bytes | None = None  # PNG, high-res crop around (x, y); None if no coordinates
    detail_width: int = 0
    detail_height: int = 0
    detail_crop_info: str = ""  # e.g. "crop at (340,220) size 400x400"


def make_overview_and_detail(
    native_img: Image.Image,
    event_x: int | None,
    event_y: int | None,
    display_width: int = 0,
    display_origin: tuple[int, int] = (0, 0),
    scale_factor: float = 0,
) -> tuple[bytes, int, int, bytes | None, int, int, str]:
    """Build (overview_bytes, ow, oh, detail_bytes, dw, dh, detail_info) from a native PIL image.

    Shared by:
      - extract_frame_pair (legacy video → ffmpeg-extracted frame)
      - recorder.screenshotter.ScreenshotCapturer (v2 live screenshot)
    """
    native_w, native_h = native_img.size

    # Overview: resize to low-res
    if native_w > OVERVIEW_MAX_WIDTH:
        ratio = OVERVIEW_MAX_WIDTH / native_w
        overview_h = int(native_h * ratio)
        overview_img = native_img.resize((OVERVIEW_MAX_WIDTH, overview_h), Image.LANCZOS)
    else:
        overview_img = native_img.copy()

    overview_buf = io.BytesIO()
    overview_img.save(overview_buf, format="JPEG", quality=JPEG_QUALITY)
    overview_bytes = overview_buf.getvalue()

    detail_bytes: bytes | None = None
    detail_w, detail_h = 0, 0
    detail_info = ""

    if event_x is not None and event_y is not None:
        rel_x = event_x - display_origin[0]
        rel_y = event_y - display_origin[1]
        if scale_factor > 0:
            scale = scale_factor
        else:
            scale = native_w / display_width if display_width > 0 else 1.0
        cx = int(rel_x * scale)
        cy = int(rel_y * scale)

        crop_w = max(DETAIL_MIN_SIZE, min(DETAIL_CROP_SIZE, native_w // 3))
        crop_h = max(DETAIL_MIN_SIZE, min(DETAIL_CROP_SIZE, native_h // 3))

        left = max(0, min(cx - crop_w // 2, native_w - crop_w))
        top = max(0, min(cy - crop_h // 2, native_h - crop_h))
        right = min(native_w, left + crop_w)
        bottom = min(native_h, top + crop_h)

        detail_img = native_img.crop((left, top, right, bottom))
        detail_buf = io.BytesIO()
        detail_img.save(detail_buf, format="JPEG", quality=JPEG_QUALITY)
        detail_bytes = detail_buf.getvalue()
        detail_w, detail_h = detail_img.size
        detail_info = f"crop at ({cx},{cy}) region ({left},{top})-({right},{bottom})"

    return (
        overview_bytes,
        overview_img.size[0],
        overview_img.size[1],
        detail_bytes,
        detail_w,
        detail_h,
        detail_info,
    )


def extract_frame_at_timestamp(
    video_path: Path,
    timestamp_sec: float,
    output_path: Path,
    *,
    max_width: int | None = None,
) -> bool:
    """Extract a single frame from a video using ffmpeg.

    Returns True if successful.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{timestamp_sec:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
    ]
    if max_width:
        cmd.extend(["-vf", f"scale='min({max_width},iw)':-2"])
    cmd.append(str(output_path))

    try:
        subprocess.run(cmd, capture_output=True, timeout=15, check=True)
        return output_path.exists() and output_path.stat().st_size > 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def extract_frame_pair(
    video_path: Path,
    event: CompactedEvent,
    work_dir: Path,
    frame_index: int,
    display_width: int = 0,
    display_origin: tuple[int, int] = (0, 0),
    scale_factor: float = 0,
) -> FramePair | None:
    """Extract overview + detail image pair for a single event.

    Args:
        video_path: Path to the recording video.
        event: The compacted event to extract frames for.
        work_dir: Directory to write temporary files.
        frame_index: Index for naming temp files.
        display_width: Logical display width (from events.json) for scale calculation.
        display_origin: (origin_x, origin_y) of the recorded display in global
            coordinate space.  Event coordinates are global; subtract the origin
            to get display-relative coordinates before cropping.
        scale_factor: Display scale factor from events.json (e.g. 2.0 for Retina).
            When provided, used directly instead of computing from video/display
            width ratio.

    Steps:
      1. Extract full frame at native resolution
      2. Create overview by resizing to OVERVIEW_MAX_WIDTH
      3. If event has (x, y): convert global→display-relative, scale to
         physical coords, crop a region
    """
    native_path = work_dir / f"native_{frame_index:04d}.png"
    timestamp_sec = event.timestamp

    # Extract native-resolution frame
    if not extract_frame_at_timestamp(video_path, timestamp_sec, native_path):
        # Retry at slightly earlier times
        for offset in [-0.25, -1.0, 0.5]:
            alt_ts = max(0, timestamp_sec + offset)
            if extract_frame_at_timestamp(video_path, alt_ts, native_path):
                break
        else:
            return None

    try:
        native_img = Image.open(native_path)
    except Exception:
        return None

    overview_bytes, ow, oh, detail_bytes, dw, dh, detail_info = make_overview_and_detail(
        native_img, event.x, event.y, display_width, display_origin, scale_factor
    )

    # Clean up native frame
    try:
        native_path.unlink()
    except Exception:
        pass

    return FramePair(
        event=event,
        overview_bytes=overview_bytes,
        overview_width=ow,
        overview_height=oh,
        detail_bytes=detail_bytes,
        detail_width=dw,
        detail_height=dh,
        detail_crop_info=detail_info,
    )

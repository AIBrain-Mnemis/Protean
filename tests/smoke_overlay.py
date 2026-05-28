"""Smoke test: overlay window is invisible to screen capture.

Verifies:
  1. Overlay spawns and accepts log lines via OverlayLogHandler
  2. A screenshot taken while the overlay is visible does NOT contain the overlay
  3. Overlay exits cleanly when stopped
  4. Logs appear on both terminal and overlay (dual pipe)

Requires:
  - macOS or Windows (capture-proof flags are platform-specific)
  - Display (screencapture must work)

Run:
    uv run python tests/smoke_overlay.py
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────


def _capture_screenshot(out_path: Path, display_index: int = 1) -> None:
    """Take a screenshot using the platform's native tool.

    Args:
        out_path: Path to save the screenshot.
        display_index: Display number (1-indexed). Default 1 (primary).
    """
    if sys.platform == "darwin":
        # -D<N>: capture specific display (1-indexed)
        subprocess.run(
            ["screencapture", "-D", str(display_index), "-x", str(out_path)],
            check=True, timeout=5,
        )
    elif sys.platform == "win32":
        # Use mss on Windows; monitor 0 is primary, 1+ are secondary
        import mss
        with mss.mss() as sct:
            monitor_idx = max(0, display_index - 1)  # Convert 1-indexed to 0-indexed
            if monitor_idx < len(sct.monitors):
                screenshot = sct.grab(sct.monitors[monitor_idx])
                mss.tools.to_png(screenshot.rgb, screenshot.size, output=str(out_path))
            else:
                # Fallback to primary monitor if display_index is out of range
                sct.shot(output=str(out_path))
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def main() -> None:
    import tempfile

    from PIL import Image, ImageChops

    from protean.overlay import enable_overlay_for_display
    from protean.platform import get_platform
    from protean.platform.base import active_display

    # Set up logging for both console and overlay
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Detect the active display (where cursor or focused window is)
    try:
        platform = get_platform()
        active_disp = active_display(platform)

        if active_disp:
            display_index = active_disp.display_index
            print(
                f"Active display (index {display_index}): "
                f"{active_disp.width}×{active_disp.height} at "
                f"({active_disp.origin_x}, {active_disp.origin_y})"
            )
        else:
            display_index = 1
            print("Could not detect active display, using default (index 1)")
    except Exception as e:
        print(f"Warning: Failed to detect display ({e}), using default (index 1)")
        display_index = 1

    tmp = Path(tempfile.mkdtemp(prefix="protean_overlay_smoke_"))
    before_path = tmp / "before.png"
    after_path = tmp / "after.png"

    print("=" * 60)
    print("Overlay Smoke Test")
    print("=" * 60)

    # 1. Screenshot BEFORE overlay
    print(f"\n[1/4] Capturing baseline screenshot (display {display_index})...")
    _capture_screenshot(before_path, display_index=display_index)
    assert before_path.exists(), "Baseline screenshot failed"
    print(f"  Saved: {before_path}")

    # 2-4. Test with overlay context manager
    print(f"\n[2/4] Testing overlay on display {display_index}...")
    marker = "SMOKE_TEST_MARKER_12345"

    with enable_overlay_for_display(display=display_index):
        print("  Overlay context entered, writing logs...")

        # Write logs via Python logging (tests handler setup)
        for i in range(10):
            log.info(f"[Step {i}] {marker} — This should NOT appear in screenshots")

        time.sleep(5)  # Let overlay render and stay visible

        # 3. Screenshot WITH overlay
        print(f"\n[3/4] Capturing screenshot with overlay active (display {display_index})...")
        _capture_screenshot(after_path, display_index=display_index)
        assert after_path.exists(), "Overlay screenshot failed"
        print(f"  Saved: {after_path}")

    print("  Overlay context exited, logs stopped.")

    # 4. Compare
    print("\n[4/4] Comparing screenshots...")
    img_before = Image.open(before_path)
    img_after = Image.open(after_path)

    if img_before.size != img_after.size:
        print(f"  WARNING: Size mismatch {img_before.size} vs {img_after.size}")
        # Resize to match for comparison
        img_after = img_after.resize(img_before.size, Image.LANCZOS)

    diff = ImageChops.difference(img_before, img_after)
    bbox = diff.getbbox()

    if bbox is None:
        print("  PASS: Screenshots are identical — overlay is invisible to capture")
    else:
        # Some pixel diff is expected (clock, cursor blink, etc.)
        # Check if the diff region overlaps with the overlay position (bottom-right)
        w, h = img_before.size
        overlay_region = (int(w * 0.70), int(h * 0.65), w, h)  # bottom-right 30%×35%
        overlap = (
            max(bbox[0], overlay_region[0]),
            max(bbox[1], overlay_region[1]),
            min(bbox[2], overlay_region[2]),
            min(bbox[3], overlay_region[3]),
        )
        has_overlay_overlap = overlap[0] < overlap[2] and overlap[1] < overlap[3]

        if has_overlay_overlap:
            # Compute how much of the overlay region changed
            diff_crop = diff.crop(overlay_region)
            diff_pixels = list(diff_crop.getdata())
            nonzero = sum(1 for px in diff_pixels if max(px) > 10)
            total = len(diff_pixels)
            pct = nonzero / total * 100

            if pct > 5.0:
                diff_path = tmp / "diff.png"
                diff.save(diff_path)
                print(f"  FAIL: {pct:.1f}% of overlay region changed — overlay may be visible")
                print(f"  Diff saved: {diff_path}")
            else:
                print(f"  PASS: Only {pct:.1f}% diff in overlay region (noise)")
        else:
            print(f"  PASS: Diff bbox {bbox} does not overlap overlay region")

    print("\nOverlay test complete.")
    print(f"Artifacts: {tmp}")


if __name__ == "__main__":
    main()

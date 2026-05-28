"""Overlay subprocess entry point.

Creates a platform-native window that:
- Displays log lines received on stdin
- Is invisible to screen capture APIs (screencapture, mss, dxcam, BitBlt, DXGI)
- Is click-through (mouse events pass to windows behind)
- Stays on top of all windows, visible on all Spaces / virtual desktops
- Has no Dock icon (macOS) / taskbar entry (Windows)

Lifecycle: parent writes lines to stdin. When stdin closes (parent exits or crashes),
the subprocess exits.

Run as: python -m protean.overlay.window
"""

import sys
import threading

_MAX_LINES = 1000


def _get_windows_monitors() -> list[tuple[int, int, int, int]]:
    """Return monitor rects as (left, top, right, bottom), ordered by position."""
    import ctypes

    monitors: list[tuple[int, int, int, int]] = []

    def callback(hmonitor, hdc, lprect, lparam):  # type: ignore[no-untyped-def]
        r = lprect.contents
        monitors.append((r.left, r.top, r.right, r.bottom))
        return True

    import ctypes.wintypes

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_double,
    )
    ctypes.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    monitors.sort(key=lambda m: (m[1], m[0]))
    return monitors


def _run_macos(display_index: int) -> None:
    """AppKit-based overlay window for macOS."""
    import queue

    import AppKit
    from Foundation import NSAttributedString, NSMakeRect, NSObject, NSTimer

    from protean.platform.macos import configure_capture_proof_window

    line_queue: queue.Queue[str | None] = queue.Queue()

    def read_stdin() -> None:
        try:
            for line in sys.stdin:
                line_queue.put(line.rstrip("\n"))
        except (EOFError, ValueError):
            pass
        line_queue.put(None)

    threading.Thread(target=read_stdin, daemon=True).start()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    # Pick the target screen by display_index (1-based)
    screens = AppKit.NSScreen.screens()
    if display_index <= len(screens):
        screen = screens[display_index - 1]
    else:
        screen = AppKit.NSScreen.mainScreen()
    sf = screen.visibleFrame()
    win_w = max(400, int(sf.size.width * 0.25))
    win_h = max(250, int(sf.size.height * 0.30))
    margin = 20
    x = sf.origin.x + sf.size.width - win_w - margin
    y = sf.origin.y + margin  # NSWindow origin is bottom-left

    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, win_w, win_h),
        AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    window.setTitle_("Protean Log")
    window.setLevel_(AppKit.NSStatusWindowLevel)
    window.setAlphaValue_(0.85)
    configure_capture_proof_window(window)

    # Scroll view + text view
    content_rect = window.contentView().bounds()
    scroll = AppKit.NSScrollView.alloc().initWithFrame_(content_rect)
    scroll.setHasVerticalScroller_(True)
    scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

    text_view = AppKit.NSTextView.alloc().initWithFrame_(scroll.contentView().bounds())
    text_view.setEditable_(False)
    text_view.setSelectable_(True)
    text_view.setRichText_(False)
    bg = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.118, 0.118, 0.118, 1.0)
    fg = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.8, 0.8, 0.8, 1.0)
    text_view.setBackgroundColor_(bg)
    text_view.setTextColor_(fg)
    mono = AppKit.NSFont.fontWithName_size_("Menlo", 11)
    text_view.setFont_(mono)
    text_view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    text_view.textContainer().setWidthTracksTextView_(True)

    scroll.setDocumentView_(text_view)
    window.contentView().addSubview_(scroll)
    window.orderFront_(None)

    text_attrs = {
        AppKit.NSForegroundColorAttributeName: fg,
        AppKit.NSFontAttributeName: mono,
    }

    class Poller(NSObject):
        def pollLines_(self, timer: object) -> None:
            drained = False
            while True:
                try:
                    line = line_queue.get_nowait()
                except queue.Empty:
                    break
                if line is None:
                    app.terminate_(None)
                    return

                # Check for title update
                if line.startswith("@@TITLE:"):
                    new_title = line[8:]  # Strip "@@TITLE:" prefix
                    window.setTitle_(new_title)
                    drained = True
                    continue

                attr_str = NSAttributedString.alloc().initWithString_attributes_(
                    line + "\n", text_attrs,
                )
                text_view.textStorage().appendAttributedString_(attr_str)
                drained = True

            if drained:
                storage = text_view.textStorage()
                full = storage.string()
                count = full.count("\n")
                if count > _MAX_LINES:
                    remove = count - _MAX_LINES
                    idx = 0
                    for _ in range(remove):
                        idx = full.index("\n", idx) + 1
                    storage.deleteCharactersInRange_((0, idx))
                text_view.scrollToEndOfDocument_(None)

    poller = Poller.alloc().init()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.1, poller, "pollLines:", None, True,
    )
    app.run()


def _run_windows(display_index: int) -> None:
    """Tkinter-based overlay window for Windows."""
    import ctypes
    import tkinter as tk
    from tkinter import font as tkfont

    from protean.platform.windows import configure_capture_proof_window

    root = tk.Tk()
    root.title("Protean Log")

    # Get monitor geometry for the target display
    monitors = _get_windows_monitors()
    if display_index <= len(monitors):
        mon = monitors[display_index - 1]
    else:
        default_mon = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        mon = monitors[0] if monitors else default_mon
    mon_x, mon_y, mon_r, mon_b = mon
    mon_w = mon_r - mon_x
    mon_h = mon_b - mon_y
    win_w = max(400, int(mon_w * 0.25))
    win_h = max(250, int(mon_h * 0.30))
    margin = 20
    x = mon_x + mon_w - win_w - margin
    y = mon_y + mon_h - win_h - margin
    root.geometry(f"{win_w}x{win_h}+{x}+{y}")

    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.85)
    root.configure(bg="#1e1e1e")

    mono = tkfont.Font(family="Consolas", size=11)
    text = tk.Text(
        root, bg="#1e1e1e", fg="#cccccc", font=mono, wrap="word",
        state="disabled", borderwidth=0, highlightthickness=0, padx=8, pady=8,
    )
    text.pack(fill="both", expand=True)

    # Get HWND and apply capture-proof flags
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    if hwnd == 0:
        hwnd = root.winfo_id()
    configure_capture_proof_window(hwnd)

    def append_line(line: str) -> None:
        # Check for title update
        if line.startswith("@@TITLE:"):
            new_title = line[8:]  # Strip "@@TITLE:" prefix
            root.title(new_title)
            return

        text.configure(state="normal")
        text.insert("end", line + "\n")
        line_count = int(text.index("end-1c").split(".")[0])
        if line_count > _MAX_LINES:
            text.delete("1.0", f"{line_count - _MAX_LINES}.0")
        text.see("end")
        text.configure(state="disabled")

    def read_stdin() -> None:
        try:
            for line in sys.stdin:
                root.after(0, append_line, line.rstrip("\n"))
        except (EOFError, ValueError):
            pass
        try:
            root.after(0, root.destroy)
        except tk.TclError:
            pass

    threading.Thread(target=read_stdin, daemon=True).start()
    root.mainloop()


def main() -> None:
    if sys.platform == "win32" and hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    display_index = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    if sys.platform == "darwin":
        _run_macos(display_index)
    elif sys.platform == "win32":
        _run_windows(display_index)
    else:
        print(f"Unsupported platform: {sys.platform}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

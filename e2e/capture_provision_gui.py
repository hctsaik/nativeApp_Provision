"""Screenshot the real provision GUI driving a real build.

Nothing is staged: we open the actual Tk window, fill the actual fields, press
the actual buttons and photograph what appears. Modal dialogs are suppressed
(they would block the capture loop), so what you see is the window itself.

    py -3.11 e2e\\capture_provision_gui.py [output_dir]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from tkinter import messagebox

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import provision_gui  # noqa: E402

NATIVE_APP = Path(r"C:\code\claude\nativeApp")
FIXTURE = ROOT / "e2e" / "fixtures" / "portable-streamlit-smoke"


def pump(app, seconds: float) -> None:
    """Keep Tk responsive while we wait, so the window paints and its event
    queue (progress bar, log lines) drains as it would for a real operator."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        time.sleep(0.05)


def shot(app, out: Path, name: str) -> Path:
    app.lift()
    app.attributes("-topmost", True)
    pump(app, 0.6)
    # Tk reports logical (DPI-scaled) coordinates while ImageGrab works in physical
    # pixels. On a 125% display that mismatch crops the right side of the window.
    scale = app.winfo_fpixels("1i") / 72.0
    x, y = app.winfo_rootx(), app.winfo_rooty()
    box = tuple(round(v * scale) for v in
                (x, y, x + app.winfo_width(), y + app.winfo_height()))
    image = ImageGrab.grab(bbox=box, all_screens=True)
    app.attributes("-topmost", False)
    path = out / name
    image.save(path)
    print(f"[shot] {name} ({image.width}x{image.height}, dpi scale {scale:.2f})")
    return path


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "e2e" / "streamlit-desktop-gui"
    out.mkdir(parents=True, exist_ok=True)

    # A modal would freeze the capture loop; record instead of showing.
    seen: list[tuple[str, str]] = []
    messagebox.showinfo = lambda title, message, **_k: seen.append(("info", message))
    messagebox.showerror = lambda title, message, **_k: seen.append(("error", message))
    messagebox.showwarning = lambda title, message, **_k: seen.append(("warn", message))

    app = provision_gui.ProvisionApp()
    app.geometry("1000x760")

    notebook = app.nametowidget(app.winfo_children()[0].winfo_children()[2])
    notebook.select(1)  # the Streamlit desktop tab
    pump(app, 0.5)
    shot(app, out, "01-gui-empty-tab.png")

    # Exactly what picking the folder in the file dialog does — shell, runtime,
    # output, app name and entry file are all derived, never typed.
    app._apply_project(FIXTURE)
    pump(app, 0.4)
    shot(app, out, "02-gui-filled.png")

    app._start_desktop_check()
    pump(app, 0.6)
    shot(app, out, "03-gui-checked.png")

    app._start_desktop_build()
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        pump(app, 0.5)
        if app._worker is None or not app._worker.is_alive():
            pump(app, 1.5)  # let the queued done-event drain into the log
            break
    shot(app, out, "04-gui-built.png")

    ok = any(kind == "info" for kind, _ in seen)
    print(f"[capture] build dialog: {seen[-1] if seen else 'none'}")
    app.destroy()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

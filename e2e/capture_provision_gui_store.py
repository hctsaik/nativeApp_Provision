"""Screenshot the admin half of the STORE flow — the real GUI, doing real builds.

Two builds on purpose: v1.0.0 creates the shared runtime, v1.1.0 must visibly
REUSE it ("跳過 457MB 安裝" in the log). That log line is the whole point of the
store layout, so the guide shows a photograph of it rather than a claim.

    py -3.11 e2e\\capture_provision_gui_store.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from tkinter import messagebox

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import provision_gui  # noqa: E402

FIXTURE = ROOT / "e2e" / "fixtures" / "portable-streamlit-smoke"
STORE_ROOT = ROOT / "dist" / "streamlit-store-gui"
OUT = ROOT / "e2e" / "streamlit-desktop-store-gui"


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        time.sleep(0.05)


def shot(app, name: str) -> Path:
    app.lift()
    app.attributes("-topmost", True)
    pump(app, 0.6)
    scale = app.winfo_fpixels("1i") / 72.0   # Tk is logical, ImageGrab is physical
    x, y = app.winfo_rootx(), app.winfo_rooty()
    box = tuple(round(v * scale) for v in
                (x, y, x + app.winfo_width(), y + app.winfo_height()))
    image = ImageGrab.grab(bbox=box, all_screens=True)
    app.attributes("-topmost", False)
    path = OUT / name
    image.save(path)
    print(f"[shot] {name}", flush=True)
    return path


def wait_build(app, seconds: float = 900) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pump(app, 0.5)
        if app._worker is None or not app._worker.is_alive():
            pump(app, 1.5)   # let the queued done-event drain into the log
            return
    raise SystemExit("build did not finish in time")


def write_app(label: str) -> None:
    (FIXTURE / "app.py").write_text(
        "import streamlit as st\n"
        "st.set_page_config(page_title='Portable Streamlit smoke test', layout='wide')\n"
        "st.title('Portable Streamlit smoke test')\n"
        f"st.write('READY {label}')\n"
        f"st.caption('這個視窗正在執行版本 {label}(store 佈局,共用 runtime)。')\n",
        encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if STORE_ROOT.exists():
        shutil.rmtree(STORE_ROOT)

    seen: list = []
    messagebox.showinfo = lambda title, message, **_k: seen.append(("info", message))
    messagebox.showerror = lambda title, message, **_k: seen.append(("error", message))
    messagebox.showwarning = lambda title, message, **_k: seen.append(("warn", message))

    app = provision_gui.ProvisionApp()
    app.geometry("1000x780")
    notebook = app.nametowidget(app.winfo_children()[0].winfo_children()[2])
    notebook.select(1)
    pump(app, 0.5)

    # ── v1.0.0: fill the form in store mode ─────────────────────────────────
    write_app("v1.0.0")
    app._apply_project(FIXTURE)
    app.sd_output_var.set(str(STORE_ROOT))
    app.sd_store_var.set(True)
    app.sd_version_var.set("v1.0.0")
    pump(app, 0.4)
    shot(app, "01-gui-store-form.png")

    app._start_desktop_build()
    pump(app, 2.0)
    shot(app, "02-gui-store-building.png")
    wait_build(app)
    shot(app, "03-gui-store-built.png")

    # ── v1.1.0: same lock → the log must say the runtime was skipped ────────
    write_app("v1.1.0")
    app.sd_version_var.set("v1.1.0")
    pump(app, 0.3)
    app._start_desktop_build()
    wait_build(app)
    shot(app, "04-gui-store-reuse.png")

    log_text = app.sd_log.get("1.0", "end")
    reused = "跳過" in log_text
    pending = "pending=v1.1.0" in log_text
    print(f"[capture] runtime reused in log: {reused}; pending set: {pending}")
    for kind, message in seen[-2:]:
        print(f"[capture] dialog {kind}: {message.splitlines()[0]}")
    app.destroy()
    return 0 if (reused and pending) else 1


if __name__ == "__main__":
    raise SystemExit(main())

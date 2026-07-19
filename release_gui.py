#!/usr/bin/env python3
"""CIM 平台版本發佈 GUI — release.py 的薄殼（發布人員用，不需要記 CLI）。

    py -3.11 release_gui.py        （或雙擊 start-release-gui.bat）

設計來源：multi-agent 裁決規格（2026-07-19）。原則：
- 每顆按鈕背後就是一條 release.py 指令（紀錄區印出的指令可直接複製到主控台重跑）。
- 邏輯全部在 src/provision_builder/release_gui_backend.py（有單元測試）；本檔只有畫面。
- 由上而下 = 旅程（一次性金鑰 → 發版 → 晉升 → 交付）；主要按鈕緊貼欄位正下方（高 DPI）。
- 完成/失敗/取消對話框文字只准來自 RunResult.summary() 與後端文案函式——GUI 不拼接樂觀字串。
- 失敗/取消自動清掉「本次才出現」的半套產物，同一版本可直接重跑。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from provision_builder.release_gui_backend import (  # noqa: E402
    PromotePlan,
    ReleasePlan,
    StepRunner,
    WorkspaceState,
    default_keys_dir,
    delivery_instructions,
    detect_state,
    keygen_command,
    list_releases,
    promotable_releases,
    release_done_note,
    verify_command,
)

DEFAULT_WORKSPACE = Path(os.environ.get("CIM_RELEASE_WORKSPACE",
                                        Path.home() / "cim-release-workspace"))
_PREFS = Path.home() / ".cim-release-gui.json"


def _load_pref_workspace() -> Path:
    try:
        return Path(json.loads(_PREFS.read_text(encoding="utf-8"))["workspace"])
    except (OSError, ValueError, KeyError):
        return DEFAULT_WORKSPACE


def _save_pref_workspace(path: Path) -> None:
    try:
        _PREFS.write_text(json.dumps({"workspace": str(path)}), encoding="utf-8")
    except OSError:
        pass  # 偏好存不了不影響發版


class ReleaseGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CIM 平台版本發佈")
        self.geometry("880x720")
        self.minsize(760, 580)

        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._runner: StepRunner | None = None
        self._worker: threading.Thread | None = None
        self._state: WorkspaceState | None = None
        self._last_production: Path | None = None
        self._busy_since: float | None = None

        self._build_widgets()
        self.after(100, self._drain_events)
        self.after(1000, self._tick_elapsed)
        self._refresh_state()

    # ── 畫面 ─────────────────────────────────────────────────────────────

    def _build_widgets(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=4)

        banner = ttk.LabelFrame(top, text="現況（每次重讀磁碟，不憑記憶）")
        banner.pack(fill="x", pady=(4, 8))
        self.var_status = tk.StringVar(value="偵測中…")
        ttk.Label(banner, textvariable=self.var_status, justify="left").pack(
            side="left", padx=8, pady=6)
        ttk.Button(banner, text="重新偵測", command=self._refresh_state).pack(
            side="right", padx=8, pady=4)

        grid = ttk.Frame(top)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text="發佈工作區").grid(row=0, column=0, sticky="w")
        self.var_workspace = tk.StringVar(value=str(_load_pref_workspace()))
        ttk.Entry(grid, textvariable=self.var_workspace).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(grid, text="選…", width=5,
                   command=lambda: self._pick_dir(self.var_workspace)).grid(row=0, column=2)
        ttk.Label(grid, text="平台專案").grid(row=1, column=0, sticky="w")
        self.var_platform = tk.StringVar()
        ttk.Entry(grid, textvariable=self.var_platform).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(grid, text="選…", width=5,
                   command=lambda: self._pick_dir(self.var_platform)).grid(row=1, column=2)

        # 步驟 0：一次性金鑰
        prep = ttk.LabelFrame(top, text="步驟 0｜一次性：發行金鑰")
        prep.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(prep)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="key id").pack(side="left")
        self.var_key_id = tk.StringVar(value="fab-team")
        self.ent_key_id = ttk.Entry(row, textvariable=self.var_key_id, width=18)
        self.ent_key_id.pack(side="left", padx=6)
        self.btn_keygen = ttk.Button(row, text="建立發行金鑰", command=self._do_keygen)
        self.btn_keygen.pack(side="left", padx=6)
        self.var_key_note = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.var_key_note, foreground="#555").pack(side="left", padx=8)

        # 步驟 1：發版
        rel = ttk.LabelFrame(top, text="步驟 1｜發一版（打包 → 簽章 → 組 release → 驗證）")
        rel.pack(fill="x", pady=(8, 0))
        row2 = ttk.Frame(rel)
        row2.pack(fill="x", padx=8, pady=6)
        ttk.Label(row2, text="版本號").pack(side="left")
        self.var_version = tk.StringVar(value="1.0.0")
        entry = ttk.Entry(row2, textvariable=self.var_version, width=12)
        entry.pack(side="left", padx=6)
        self.var_release_preview = tk.StringVar(value="將建立：internal-1.0.0")
        ttk.Label(row2, textvariable=self.var_release_preview, foreground="#555").pack(
            side="left", padx=6)
        self.var_version.trace_add("write", lambda *_: self.var_release_preview.set(
            f"將建立：internal-{self.var_version.get().strip() or '?'}"))
        self.btn_release = ttk.Button(row2, text="發佈這一版（約 70–90 秒）",
                                      command=self._do_release)
        self.btn_release.pack(side="left", padx=8)
        self.btn_cancel = ttk.Button(row2, text="取消", command=self._do_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=4)

        # 步驟 2：晉升
        promo = ttk.LabelFrame(top, text="步驟 2｜晉升 production（強制重驗＋驗章）")
        promo.pack(fill="x", pady=(8, 0))
        row3 = ttk.Frame(promo)
        row3.pack(fill="x", padx=8, pady=6)
        ttk.Label(row3, text="來源").pack(side="left")
        self.var_promote_src = tk.StringVar()
        self.cmb_promote = ttk.Combobox(row3, textvariable=self.var_promote_src, width=32,
                                        state="readonly")
        self.cmb_promote.pack(side="left", padx=6)
        self.btn_reverify = ttk.Button(row3, text="重新驗證（6 秒）", command=self._do_reverify)
        self.btn_reverify.pack(side="left", padx=4)
        self.btn_promote = ttk.Button(row3, text="晉升到 production", command=self._do_promote)
        self.btn_promote.pack(side="left", padx=6)

        # 步驟 3：交付
        ship = ttk.LabelFrame(top, text="步驟 3｜交付現場")
        ship.pack(fill="x", pady=(8, 0))
        row4 = ttk.Frame(ship)
        row4.pack(fill="x", padx=8, pady=6)
        self.var_ship = tk.StringVar(value="（先完成步驟 2）")
        ttk.Label(row4, textvariable=self.var_ship).pack(side="left")
        self.btn_open = ttk.Button(row4, text="開啟 production 資料夾",
                                   command=self._open_production, state="disabled")
        self.btn_open.pack(side="left", padx=8)
        self.btn_copy = ttk.Button(row4, text="複製交付指示",
                                   command=self._copy_instructions, state="disabled")
        self.btn_copy.pack(side="left", padx=4)
        ttk.Label(row4, text="金鑰目錄（私鑰）絕不複製出去", foreground="#b3261e").pack(
            side="left", padx=10)

        # 紀錄
        logf = ttk.LabelFrame(self, text="執行輸出（> 開頭那行 = 實際執行的指令，可複製重跑）")
        logf.pack(fill="both", expand=True, padx=10, pady=(8, 2))
        self.txt = tk.Text(logf, height=12, bg="#0f1419", fg="#d8e0e8",
                           insertbackground="#d8e0e8", font=("Consolas", 9), wrap="none")
        self.txt.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(logf, command=self.txt.yview)
        scroll.pack(side="right", fill="y")
        self.txt.configure(yscrollcommand=scroll.set, state="disabled")

        self.var_elapsed = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.var_elapsed, foreground="#555").pack(
            anchor="w", padx=12, pady=(0, 8))

    # ── 狀態偵測 ──────────────────────────────────────────────────────────

    def _refresh_state(self) -> None:
        workspace = Path(self.var_workspace.get())
        _save_pref_workspace(workspace)
        candidates = ([self.var_platform.get()] if self.var_platform.get() else []) + \
            ["C:/code/claude/nativeApp"]
        state = detect_state(workspace, platform_candidates=candidates)
        self._state = state

        releases = list_releases(workspace / "releases")
        latest_internal = next((r for r in releases if r.channel == "internal"), None)
        latest_production = next((r for r in releases if r.channel == "production"), None)

        parts = [f"金鑰：{'OK（key_id=' + str(state.key_id) + '，在 ' + str(default_keys_dir()) + '）' if state.keys_ready else '尚未建立 → 先做步驟 0'}"]
        if state.platform_root:
            shell = "殼 OK" if state.shell_exe else \
                "缺 prebuilt\\cim-light.exe → 非 WDAC 機器跑 scripts\\win\\build-shell.bat 後複製就位"
            parts.append(f"平台：{state.platform_root}（{shell}）")
            if not self.var_platform.get():
                self.var_platform.set(str(state.platform_root))
        else:
            parts.append("平台：找不到 → 選一個含 sidecar\\python-engine\\engine.py 的資料夾")
        parts.append("上次發版："
                     + (latest_internal.release_id if latest_internal else "（無）")
                     + " / "
                     + (latest_production.release_id if latest_production else "（無 production）")
                     + f"　建議下一版 {state.suggested_version}")
        next_step = "步驟 0（建立金鑰）" if not state.keys_ready else \
            ("步驟 2（晉升）" if promotable_releases(releases) else "步驟 1（發一版）")
        parts.append(f">> 下一步：{next_step}")
        self.var_status.set("\n".join(parts))

        self.var_version.set(state.suggested_version)
        self.var_key_note.set(
            f"沿用 {state.key_file.name}（在 {default_keys_dir()}）" if state.key_file else
            f"將建立於 {default_keys_dir()}（工作區之外，不會跟 releases 一起被複製）")
        keygen_state = "disabled" if state.keys_ready else "normal"
        self.btn_keygen.configure(state=keygen_state)
        self.ent_key_id.configure(state=keygen_state)

        names = [r.release_id for r in promotable_releases(releases)]
        self.cmb_promote.configure(values=names)
        self.var_promote_src.set(names[0] if names else "")

        if latest_production:
            self._last_production = latest_production.path
            self.var_ship.set(f"{latest_production.release_id} 已就緒")
            self.btn_open.configure(state="normal")
            self.btn_copy.configure(state="normal")
        else:
            self.var_ship.set("（先完成步驟 2）")
            self.btn_open.configure(state="disabled")
            self.btn_copy.configure(state="disabled")

    # ── 動作 ─────────────────────────────────────────────────────────────

    def _do_keygen(self) -> None:
        key_id = self.var_key_id.get().strip()
        if not key_id:
            messagebox.showerror("缺 key id", "先填 key id（例：fab-team）")
            return
        keys = default_keys_dir()
        self._run_steps(
            [("建立發行金鑰", keygen_command(key_id))],
            done_note=(f"私鑰：{keys / (key_id + '.private.json')}\n"
                       f"信任清單：{keys / 'trusted_publishers.json'}\n"
                       "私鑰只留發佈機（勿進 repo/交付包/裝置）；"
                       "trusted_publishers.json 之後隨部署放到 User 機器。"))

    def _do_release(self) -> None:
        state = self._state
        if state is None or not self.var_platform.get():
            messagebox.showerror("還不能發版", "先在上方選到正確的平台專案，再按「重新偵測」。")
            return
        plan = ReleasePlan(
            workspace=Path(self.var_workspace.get()),
            platform_root=Path(self.var_platform.get()),
            version=self.var_version.get().strip(),
            key_file=state.key_file or Path("missing"),
            trust_store=state.trust_store or Path("missing"),
            shell_exe=state.shell_exe,
        )
        problems = plan.problems()
        if problems:
            messagebox.showerror("先修這些再發版", "\n".join(f"· {p}" for p in problems))
            return
        self._run_steps(plan.steps(), done_note=release_done_note(plan),
                        partials=plan.partials())

    def _do_promote(self) -> None:
        state = self._state
        source_name = self.var_promote_src.get()
        if not source_name:
            messagebox.showerror("沒有可晉升的 release", "先完成步驟 1，或所有 internal 都已晉升過。")
            return
        source = Path(self.var_workspace.get()) / "releases" / source_name
        plan = PromotePlan(source, trust_store=(state.trust_store if state else None)
                           or Path("missing"))
        problems = plan.problems()
        if problems:
            messagebox.showerror("先修這些再晉升", "\n".join(f"· {p}" for p in problems))
            return
        if not messagebox.askyesno(
                "晉升到 production",
                f"將把 {source_name} 全程重新驗證＋驗章後複製成 {plan.release_id()}。\n"
                "未簽章或驗不過章會被拒絕——這是設計。\n\n繼續？"):
            return
        target = source.parent / plan.release_id()
        self._run_steps(plan.steps(), partials=plan.partials(),
                        done_note=(f"production 已就緒：{target}\n"
                                   "到「步驟 3」複製交付指示，把整個資料夾交給現場。"))

    def _do_reverify(self) -> None:
        state = self._state
        source_name = self.var_promote_src.get()
        if not source_name or state is None or state.trust_store is None:
            messagebox.showerror("無法驗證", "先選一個 release，且金鑰已建立。")
            return
        release_dir = Path(self.var_workspace.get()) / "releases" / source_name
        self._run_steps([("重新驗證", verify_command(release_dir, state.trust_store))],
                        done_note=f"{source_name} 驗證通過（僅完整性與簽章；晉升時仍會全程重驗）。")

    def _do_cancel(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            self._append("[取消] 已送出終止（整棵程序樹）…")

    def _open_production(self) -> None:
        if self._last_production and self._last_production.is_dir():
            os.startfile(self._last_production)  # noqa: S606

    def _copy_instructions(self) -> None:
        if not self._last_production:
            return
        releases = list_releases(self._last_production.parent)
        info = next((r for r in releases if r.path == self._last_production), None)
        if info is None:
            messagebox.showerror("讀不到 release", "manifest 不見了？按「重新偵測」。")
            return
        text = delivery_instructions(info)
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("已複製", "交付指示已複製到剪貼簿：\n\n" + text)

    # ── 執行緒與事件 ──────────────────────────────────────────────────────

    def _run_steps(self, steps, done_note: str, partials=()) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showwarning("正在執行", "上一個工作還在跑；先等它完成或按「取消」。")
            return
        self._set_busy(True)
        self._runner = StepRunner()

        def work() -> None:
            result = self._runner.run(steps, lambda s: self._events.put(("line", s)),
                                      partials=partials)
            self._events.put(("done", (result, done_note)))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "line":
                    self._append(str(payload))
                elif kind == "done":
                    result, done_note = payload
                    self._set_busy(False)
                    self._append("─" * 72)
                    self._append(result.summary())
                    if result.ok:
                        messagebox.showinfo("完成", result.summary() + "\n\n" + done_note)
                    elif result.cancelled:
                        messagebox.showwarning("已取消", result.summary())
                    else:
                        messagebox.showerror("失敗", result.summary())
                    self._refresh_state()
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _tick_elapsed(self) -> None:
        if self._busy_since is not None:
            self.var_elapsed.set(f"執行中…已經過 {int(time.monotonic() - self._busy_since)} 秒")
        self.after(1000, self._tick_elapsed)

    def _set_busy(self, busy: bool) -> None:
        self._busy_since = time.monotonic() if busy else None
        if not busy:
            self.var_elapsed.set("")
        state = "disabled" if busy else "normal"
        for button in (self.btn_release, self.btn_promote, self.btn_keygen, self.btn_reverify):
            button.configure(state=state)
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        if not busy:
            self._refresh_state()  # keygen 鈕等狀態由事實決定

    def _append(self, line: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _pick_dir(self, var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if chosen:
            var.set(chosen)
            self._refresh_state()


def main() -> int:
    app = ReleaseGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

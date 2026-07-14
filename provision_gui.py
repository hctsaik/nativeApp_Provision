#!/usr/bin/env python3
"""native_Provision 的獨立 Windows 打包 GUI（僅在可連網建置機執行）。"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from provision_builder import DEFAULT_BIG_THRESHOLD_MB  # noqa: E402
from provision_builder._util import human_size  # noqa: E402
from provision_builder.gui_backend import (  # noqa: E402
    BuildOptions,
    BuildProcess,
    ValidationOptions,
    ValidationProcess,
)
from provision_builder.gateway import PlatformGateway  # noqa: E402
from provision_builder.source_pack import discover_source_modules  # noqa: E402
from provision_builder.streamlit_desktop import BuildRequest  # noqa: E402
from provision_builder.streamlit_desktop import build as build_streamlit_desktop  # noqa: E402
from provision_builder.streamlit_desktop import build_into_store as build_streamlit_store  # noqa: E402
from provision_builder.streamlit_desktop import export_update as export_streamlit_update  # noqa: E402
from provision_builder.streamlit_desktop import export_full_tree as export_streamlit_full  # noqa: E402
from provision_builder.streamlit_desktop import warnings_for as streamlit_warnings_for  # noqa: E402
from provision_builder.streamlit_desktop.store_builder import (  # noqa: E402
    list_versions as list_store_versions,
    newest_version as newest_store_version,
    update_needs_runtime,
)
from provision_builder.streamlit_desktop.device.gc import run_gc as streamlit_gc  # noqa: E402
from provision_builder.streamlit_desktop import (  # noqa: E402
    default_output,
    find_entrypoint,
    find_runtime,
    find_shell,
    suggest_name,
)
from provision_builder.streamlit_desktop.discover import looks_like_streamlit  # noqa: E402
from provision_builder.streamlit_desktop import validate_request as validate_streamlit_request  # noqa: E402
from provision_builder.streamlit_desktop.builder import (  # noqa: E402
    runtime_would_be_reused,
    scan_project as scan_streamlit_project,
)
from provision_builder.streamlit_desktop.validate import (  # noqa: E402
    validate_store_request as validate_streamlit_store_request,
)

# User 端的步驟只寫一次。以前對話框說「兩步、按 Start」，交付包裡的說明檔卻寫
# 「三步、按『啟動』」——而畫面上根本沒有叫「啟動」的按鈕。管理員把說明轉給產線，
# 作業員就在找一個不存在的鈕。
USER_STEPS = (
    "  1. 雙擊 start.bat\n"
    "  2. 視窗出現後，在上方「工作流程」下拉確認選到這個 App\n"
    "  3. 按一次旁邊的「Start」按鈕，App 就會顯示在視窗裡"
)


def list_store_apps(root: Path) -> list[dict]:
    """讀一棵既有 Store 樹上有哪些 App、各自跑在哪一版。

    只讀 state.json（它就是那棵樹的事實來源），不需要這個 session 建過任何東西。
    """
    apps: list[dict] = []
    apps_dir = Path(root) / "apps"
    if not apps_dir.is_dir():
        return apps
    for child in sorted(apps_dir.iterdir()):
        state_file = child / "state" / "state.json"
        if not (child.is_dir() and state_file.is_file()):
            continue
        try:
            data = json.loads(state_file.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        apps.append({
            "app_id": child.name,
            "current": data.get("current"),
            "pending": data.get("pending"),
        })
    return apps


class ProvisionApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CIM 離線工具包產生器")
        self.geometry("920x720")
        self.minsize(780, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.project_var = tk.StringVar(value=str(ROOT.parent / "nativeApp"))
        self.dest_var = tk.StringVar(value=str(ROOT / "dist" / "provision"))
        self.module_root_var = tk.StringVar(value=str(ROOT.parent / "nativeApp" / "sidecar" / "python-engine" / "plugins" / "cim-modules" / "modules"))
        self.force_var = tk.BooleanVar(value=False)
        self.launch_mode_var = tk.StringVar(value="portable")
        self.validation_var = tk.StringVar(value=str(ROOT / "e2e" / "validation-work"))
        self.validation_tool_var = tk.StringVar()
        self.status_var = tk.StringVar(value="請選擇 CIM 平台專案，然後掃描工具。")
        self.target_var = tk.StringVar(value="目標：Windows x64 / Python 3.11 / cp311")

        # Streamlit 桌面資料夾（第二頁）——殼／runtime／輸出都自動偵測，見 _detect_environment()
        self.sd_project_var = tk.StringVar()
        self.sd_entry_var = tk.StringVar()
        self.sd_name_var = tk.StringVar()
        self.sd_output_var = tk.StringVar(value=str(default_output()))
        self.sd_shell_var = tk.StringVar()
        self.sd_runtime_var = tk.StringVar()
        self.sd_port_var = tk.StringVar(value="0")   # 0 = 隨機挑一個沒被占用的埠
        self.sd_status_var = tk.StringVar(value="選一個 Streamlit 專案，其餘會自動帶出。")
        self.sd_env_var = tk.StringVar(value="")
        self.sd_advanced_var = tk.BooleanVar(value=False)
        self.sd_lock_var = tk.StringVar()      # 指定 lock 檔（兩個 App 想共用 runtime 就靠它）
        self.sd_exclude_var = tk.StringVar()   # 額外排除樣式，分號分隔
        self.sd_extras_var = tk.StringVar()    # pyproject 的 optional-dependencies 群組
        self.sd_app_id_var = tk.StringVar()    # 應用代號（中文名字推不出代號時必填）
        self.sd_webview2_var = tk.StringVar()  # WebView2 離線安裝檔（無網目標機必備）
        self.sd_deliver_version_var = tk.StringVar()   # 要交付/更新的是哪一版
        self.sd_store_var = tk.BooleanVar(value=False)
        self.sd_version_var = tk.StringVar(value="v1.0.0")
        self.sd_update_source_var = tk.StringVar()
        self._sd_last_store: Path | None = None      # 最近一次建好的 Store 樹
        self._sd_last_app: str | None = None
        self._sd_last_version: str | None = None
        self._sd_last_package: Path | None = None    # fat 模式：剛做好的那個包
        self._sd_cancel = threading.Event()          # 取消旗標：builder 每個階段邊界都會讀
        self._tool_vars: dict[str, tk.BooleanVar] = {}
        self._tool_requires: dict[str, list[str]] = {}
        self._source_modules = {}
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._build = BuildProcess()
        self._validation = ValidationProcess()
        self._active_process = self._build

        self._make_ui()
        self._detect_environment()
        self.after(100, self._drain_events)

    def _make_ui(self) -> None:
        shell = ttk.Frame(self, padding=16)
        shell.pack(fill="both", expand=True)

        ttk.Label(shell, text="離線工具包產生器", font=("Microsoft JhengHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            shell,
            text="在可連網建置機產生可搬到離線電腦的交付物。",
        ).pack(anchor="w", pady=(2, 10))

        notebook = ttk.Notebook(shell)
        notebook.pack(fill="both", expand=True)

        # 既有的 dep-pack 流程原封不動搬進第一頁；下面的區塊照舊掛在 `outer` 上。
        # 分頁名稱直接寫出「吃什麼」——兩頁長得像，選錯頁得到的錯誤（找不到 plugin.yaml）
        # 對使用者毫無意義。
        outer = ttk.Frame(notebook, padding=10)
        notebook.add(outer, text="  CIM 平台模組（需 plugin.yaml）  ")

        desktop = ttk.Frame(notebook, padding=10)
        notebook.add(desktop, text="  Streamlit 專案 → 桌面 App  ")
        self._make_desktop_tab(desktop)

        paths = ttk.LabelFrame(outer, text="1. Module、平台與輸出", padding=10)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)
        ttk.Label(paths, text="Module 資料夾").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(paths, textvariable=self.module_root_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(paths, text="瀏覽…", command=self._browse_module_root).grid(row=0, column=2, padx=(8, 0), pady=4)
        ttk.Label(paths, text="平台專案").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(paths, textvariable=self.project_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(paths, text="瀏覽…", command=self._browse_project).grid(row=1, column=2, padx=(8, 0), pady=4)
        ttk.Label(paths, text="輸出位置").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(paths, textvariable=self.dest_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(paths, text="瀏覽…", command=self._browse_dest).grid(row=2, column=2, padx=(8, 0), pady=4)
        ttk.Label(paths, textvariable=self.target_var, foreground="#315a88").grid(row=3, column=1, sticky="w", pady=(4, 0))
        primary = ttk.Frame(paths)
        primary.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.scan_button = ttk.Button(primary, text="1. 掃描 Module", command=self._start_scan)
        self.scan_button.pack(side="left")
        self.build_button = ttk.Button(primary, text="2. 開始打包", command=self._start_build, state="disabled")
        self.build_button.pack(side="left", padx=(8, 0))
        ttk.Label(primary, text="先掃描並勾選 Module，再開始打包。", foreground="#666").pack(side="left", padx=(12, 0))

        launch = ttk.Frame(paths)
        launch.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(launch, text="一鍵啟動 bat（run-platform.bat）預設模式：").pack(side="left")
        ttk.Radiobutton(launch, text="可攜離線機", value="portable",
                        variable=self.launch_mode_var).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(launch, text="本機測試", value="dev",
                        variable=self.launch_mode_var).pack(side="left", padx=(6, 0))
        ttk.Label(launch, text="（隨包產生；平台專案路徑會自動烤入，離線機仍可改設定區切換）",
                  foreground="#666").pack(side="left", padx=(8, 0))

        tools = ttk.LabelFrame(outer, text="2. 選擇需要打包的工具", padding=10)
        tools.pack(fill="both", expand=True, pady=12)
        toolbar = ttk.Frame(tools)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="全選", command=lambda: self._select_all(True)).pack(side="left")
        ttk.Button(toolbar, text="全不選", command=lambda: self._select_all(False)).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(toolbar, text="強制重建（忽略增量快取）", variable=self.force_var).pack(side="right")

        canvas_frame = ttk.Frame(tools)
        canvas_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.canvas = tk.Canvas(canvas_frame, highlightthickness=0, height=180)
        scroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.tool_list = ttk.Frame(self.canvas)
        self.tool_list.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._tool_window = self.canvas.create_window((0, 0), window=self.tool_list, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._tool_window, width=e.width))
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Label(self.tool_list, text="尚未掃描。", foreground="#666").pack(anchor="w", pady=8)

        run = ttk.LabelFrame(outer, text="3. 建置", padding=10)
        run.pack(fill="both")
        ttk.Label(run, textvariable=self.status_var).pack(anchor="w")
        self.progress = ttk.Progressbar(run, mode="indeterminate")
        self.progress.pack(fill="x", pady=8)
        self.log = tk.Text(run, height=10, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

        validation = ttk.LabelFrame(outer, text="4. 實際套用與 Tauri 驗證（打包完成後）", padding=10)
        validation.pack(fill="x", pady=(12, 0))
        validation.columnconfigure(1, weight=1)
        ttk.Label(validation, text="驗證資料夾").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(validation, textvariable=self.validation_var).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(validation, text="瀏覽…", command=self._browse_validation).grid(row=0, column=2, padx=(8, 0), pady=3)
        ttk.Label(validation, text="驗證工具").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.validation_tool = ttk.Combobox(validation, textvariable=self.validation_tool_var, state="readonly")
        self.validation_tool.grid(row=1, column=1, sticky="ew", pady=3)
        self.validate_button = ttk.Button(validation, text="套用、暖機並啟動 Tauri 驗證", command=self._start_validation, state="disabled")
        self.validate_button.grid(row=1, column=2, padx=(8, 0), pady=3)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(12, 0))
        self.open_button = ttk.Button(buttons, text="開啟輸出資料夾", command=self._open_dest, state="disabled")
        self.open_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right")

    # ── Streamlit 桌面資料夾（簡易版）────────────────────────────────────────
    # 刻意與 dep-pack / .napp 發布分開：這一頁產出的是「複製即部署」的獨立資料夾，
    # 不進 registry、不做 rollout。設計見
    # docs/SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER_PHASE0_AND_DESIGN.md

    def _make_desktop_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="選一個 Streamlit 專案，產生「可攜 Python + Tauri 殼 + 你的專案」的單一資料夾。"
                 "User 拿到後雙擊 start.bat，不需安裝 Python、Streamlit、Node 或 Rust。",
            wraplength=820, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        form = ttk.LabelFrame(parent, text="1. 選擇 Streamlit 專案", padding=10)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        # 只有專案是「非問不可」的；其餘一律推導出來，讓使用者確認而不是輸入。
        rows = (
            ("專案資料夾", self.sd_project_var, self._browse_sd_project),
            ("應用名稱", self.sd_name_var, None),
            ("入口檔案", self.sd_entry_var, self._browse_sd_entry),
        )
        for row, (label, var, browse) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(form, textvariable=var).grid(row=row, column=1, sticky="ew", pady=3)
            if browse is not None:
                ttk.Button(form, text="瀏覽…", command=browse).grid(row=row, column=2, padx=(8, 0), pady=3)
        ttk.Label(form, text="應用名稱與入口檔案會依專案自動帶出，需要時再改。",
                  foreground="#666").grid(row=len(rows), column=1, sticky="w", pady=(2, 0))

        # 建置環境：自動偵測，不要求輸入；但一定要看得到偵測到什麼。
        env = ttk.Frame(parent)
        env.pack(fill="x", pady=(10, 0))
        ttk.Label(env, textvariable=self.sd_env_var, foreground="#17663a",
                  wraplength=700, justify="left").pack(side="left")
        self.sd_fetch_button = ttk.Button(env, text="下載可攜 Python…",
                                          command=self._start_fetch_runtime)
        ttk.Checkbutton(env, text="進階設定", variable=self.sd_advanced_var,
                        command=self._toggle_advanced).pack(side="right")

        self.sd_advanced = ttk.LabelFrame(parent, text="進階設定（自動偵測失敗或要換來源時才需要）", padding=10)
        self.sd_advanced.columnconfigure(1, weight=1)
        advanced_rows = (
            ("Tauri 殼（預建）", self.sd_shell_var, self._browse_sd_shell),
            ("可攜 Python runtime", self.sd_runtime_var, self._browse_sd_runtime),
            ("輸出位置", self.sd_output_var, self._browse_sd_output),
            # 兩個 App 要共用同一份 runtime，唯一的辦法就是餵同一份 lock。
            # 這個欄位 BuildRequest 早就有了，GUI 卻沒接出來，於是管理員只能把
            # 別人的 lock 複製進自己的 repo 才做得到。
            ("相依 lock 檔（選填）", self.sd_lock_var, self._browse_sd_lock),
        )
        for row, (label, var, browse) in enumerate(advanced_rows):
            ttk.Label(self.sd_advanced, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(self.sd_advanced, textvariable=var).grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Button(self.sd_advanced, text="瀏覽…", command=browse).grid(row=row, column=2, padx=(8, 0), pady=3)
        ttk.Label(self.sd_advanced,
                  text="不指定 lock 檔時，會依序找專案裡的 requirements.lock.txt → requirements.txt → pyproject。"
                       "兩個 App 指定同一份 lock，才會共用同一份 runtime（省幾百 MB）。",
                  foreground="#666", wraplength=640, justify="left"
                  ).grid(row=len(advanced_rows), column=1, columnspan=2, sticky="w", pady=(0, 6))

        exclude_row = ttk.Frame(self.sd_advanced)
        exclude_row.grid(row=len(advanced_rows) + 1, column=0, columnspan=3, sticky="ew")
        ttk.Label(exclude_row, text="額外排除").pack(side="left")
        ttk.Entry(exclude_row, textvariable=self.sd_exclude_var, width=30).pack(side="left", padx=(8, 0))
        ttk.Label(exclude_row, text="（分號分隔的樣式，例：data/*;*.mp4;notebooks/*。"
                                    "也可以在專案根目錄放 .provisionignore）",
                  foreground="#666", wraplength=520, justify="left").pack(side="left", padx=(8, 0))

        # 應用代號:同一棵 Store 樹上的身分證。兩個中文名字的 App 以前會推導出
        # 同一個代號(都變成 app-streamlit-app),第二個建置會被誤診成「版本衝突」,
        # 照著提示改版號 = 把產線上的 A 換成另一個程式。現在推不出來就直接要求填。
        appid_row = ttk.Frame(self.sd_advanced)
        appid_row.grid(row=len(advanced_rows) + 2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(appid_row, text="應用代號").pack(side="left")
        ttk.Entry(appid_row, textvariable=self.sd_app_id_var, width=30).pack(side="left", padx=(8, 0))
        ttk.Label(appid_row, text="（同一棵 Store 樹上的唯一識別，例：image-viewer。"
                                  "留空 = 依應用名稱自動產生；名稱沒有英數字時必填）",
                  foreground="#666", wraplength=520, justify="left").pack(side="left", padx=(8, 0))

        extras_row = ttk.Frame(self.sd_advanced)
        extras_row.grid(row=len(advanced_rows) + 3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(extras_row, text="選用相依群組").pack(side="left")
        ttk.Entry(extras_row, textvariable=self.sd_extras_var, width=30).pack(side="left", padx=(8, 0))
        ttk.Label(extras_row, text="（pyproject 的 [project.optional-dependencies]，逗號分隔，"
                                   "例：llm,dev。不填 = 只裝必要相依）",
                  foreground="#666", wraplength=520, justify="left").pack(side="left", padx=(8, 0))

        # WebView2 是這整個交付包裡「唯一」需要事先裝在目標機上的東西。無網工廠機
        # 沒有它就開不起來，而我們的自救 bat 需要這個離線安裝檔——不附，那條路是死的。
        wv_row = ttk.Frame(self.sd_advanced)
        wv_row.grid(row=len(advanced_rows) + 4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(wv_row, text="WebView2 離線安裝檔").pack(side="left")
        ttk.Entry(wv_row, textvariable=self.sd_webview2_var, width=30).pack(side="left", padx=(8, 0))
        ttk.Button(wv_row, text="瀏覽…", command=self._browse_webview2).pack(side="left", padx=(4, 0))
        ttk.Label(wv_row, text="（目標機不能上網時必附。到 go.microsoft.com/fwlink/?LinkId=2124701 "
                              "下載 Evergreen Standalone Installer）",
                  foreground="#666", wraplength=460, justify="left").pack(side="left", padx=(8, 0))

        port_row = ttk.Frame(self.sd_advanced)
        port_row.grid(row=len(advanced_rows) + 5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(port_row, text="偏好連接埠").pack(side="left")
        ttk.Entry(port_row, textvariable=self.sd_port_var, width=8).pack(side="left", padx=(8, 0))
        ttk.Label(port_row, text="（0 = 每次啟動隨機挑一個 8000–9000 之間、確認沒被占用的埠；"
                                 "填固定值則該埠被占用時仍會自動改用其他可用埠）",
                  foreground="#666", wraplength=560, justify="left").pack(side="left", padx=(8, 0))

        mode_row = ttk.Frame(parent)
        mode_row.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(
            mode_row, variable=self.sd_store_var, command=self._on_store_mode_toggled,
            text="以 Store 佈局輸出（版本化 + 共用 runtime + 自動更新/回滾；需完整釘死的 requirements）",
        ).pack(side="left")
        ttk.Label(mode_row, text="版本").pack(side="left", padx=(16, 4))
        ttk.Entry(mode_row, textvariable=self.sd_version_var, width=12).pack(side="left")
        # 「輸出位置」在兩種模式下意思不同——不講清楚，使用者會把 store 樹蓋在 fat 包上面。
        self.sd_mode_hint = ttk.Label(mode_row, text="", foreground="#8a5700", wraplength=380,
                                      justify="left")
        self.sd_mode_hint.pack(side="left", padx=(12, 0))

        # 更新來源必須在「建置前」填：它會被寫進 config.json，跟著交付包出去。
        # 放在「交付與維護」區（建置後才啟用）等於永遠來不及——那是上一版的位置。
        self.sd_source_row = ttk.Frame(parent)
        ttk.Label(self.sd_source_row, text="更新來源（選填）").pack(side="left")
        ttk.Entry(self.sd_source_row, textvariable=self.sd_update_source_var, width=34).pack(side="left", padx=(8, 0))
        ttk.Button(self.sd_source_row, text="瀏覽…", command=self._browse_update_source).pack(side="left", padx=(4, 0))
        ttk.Label(self.sd_source_row,
                  text="會寫進 config.json 跟著交付包出去，User 端據此自動更新。"
                       "請選「放各個 App 更新包的上層目錄」，例：\\\\nas\\updates。"
                       "已經交付出去的機器要改，用 admin.bat 的「設定更新來源」。",
                  foreground="#666", wraplength=420, justify="left").pack(side="left", padx=(10, 0))

        actions = self.sd_actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(12, 0))
        self.sd_check_button = ttk.Button(actions, text="檢查專案", command=self._start_desktop_check)
        self.sd_check_button.pack(side="left")
        self.sd_build_button = ttk.Button(actions, text="建立可交付資料夾", command=self._start_desktop_build)
        self.sd_build_button.pack(side="left", padx=(8, 0))
        self.sd_cancel_button = ttk.Button(actions, text="取消", command=self._cancel_desktop,
                                           state="disabled")
        self.sd_cancel_button.pack(side="left", padx=(8, 0))
        self.sd_open_button = ttk.Button(actions, text="開啟輸出資料夾",
                                         command=self._open_sd_output, state="disabled")
        self.sd_open_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, text="⚠ 建置時會連網下載相依；產出的資料夾在 User 端完全離線。",
                  foreground="#8a5700").pack(side="left", padx=(12, 0))

        # 交付與維護：這些路徑本來只有 API，GUI 完全沒有入口——管理員只好手打指令。
        self.sd_deliver = ttk.LabelFrame(parent, text="2. 交付與維護（Store 佈局）", padding=8)
        # 「要發哪一版」不能用猜的。建置機建完新版是設成「待套用」而不是 current
        # （建置機自己不會去啟動它），所以拿 current 去匯出，發出去的正是產線
        # 已經在跑的那一版——走一趟工廠，裝了個寂寞。
        version_row = ttk.Frame(self.sd_deliver)
        version_row.pack(fill="x", pady=(0, 6))
        ttk.Label(version_row, text="要交付的版本").pack(side="left")
        self.sd_deliver_version = ttk.Combobox(version_row, textvariable=self.sd_deliver_version_var,
                                               width=18, state="readonly", values=())
        self.sd_deliver_version.pack(side="left", padx=(8, 0))
        self.sd_deliver_version_hint = ttk.Label(version_row, text="", foreground="#666",
                                                 wraplength=560, justify="left")
        self.sd_deliver_version_hint.pack(side="left", padx=(10, 0))

        deliver_row = ttk.Frame(self.sd_deliver)
        deliver_row.pack(fill="x")
        # 兩個按鈕、兩件不同的事。上一版把它們塞進同一個「是／否」對話框，
        # 而「否」那條路產出的包在目標機根本裝不起來——選錯的代價太大，分開。
        self.sd_export_full_button = ttk.Button(
            deliver_row, text="匯出完整交付（給新機器）…",
            command=lambda: self._export_store(full=True), state="disabled")
        self.sd_export_full_button.pack(side="left")
        self.sd_export_update_button = ttk.Button(
            deliver_row, text="匯出更新包（給已部署的機器）…",
            command=lambda: self._export_store(full=False), state="disabled")
        self.sd_export_update_button.pack(side="left", padx=(8, 0))
        self.sd_gc_button = ttk.Button(deliver_row, text="回收未使用的版本／runtime（先試算）",
                                       command=self._gc_store, state="disabled")
        self.sd_gc_button.pack(side="left", padx=(8, 0))
        # 「交付與維護」以前只有在「這個 session 剛好建過一版」時才活著。管理員早上
        # 打開工具想把上週那一版發出去、或想清磁碟，會發現三個按鈕全是灰的，而且
        # 沒有任何地方告訴他要先重建一次——那正是他不想做的事。
        ttk.Button(deliver_row, text="開啟既有 Store 樹…",
                   command=self._open_existing_store).pack(side="left", padx=(16, 0))
        self.sd_store_info = ttk.Label(self.sd_deliver, text="", foreground="#17663a",
                                       wraplength=780, justify="left")
        self.sd_store_info.pack(anchor="w", pady=(6, 0))
        ttk.Label(self.sd_deliver,
                  text="完整交付＝整棵樹（bootstrap + start.bat + 共用 runtime + 殼 + 這一版），拿到就能跑。"
                       "更新包＝只有這一版（十幾 MB），要放進更新來源，或在目標機用 admin.bat 的"
                       "「套用已複製進來的更新包」。",
                  foreground="#666", wraplength=780, justify="left").pack(anchor="w", pady=(6, 0))

        run = ttk.LabelFrame(parent, text="3. 建置", padding=10)
        run.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(run, textvariable=self.sd_status_var).pack(anchor="w")
        self.sd_progress = ttk.Progressbar(run, mode="indeterminate")
        self.sd_progress.pack(fill="x", pady=8)
        self.sd_log = tk.Text(run, height=12, wrap="word", state="disabled", font=("Consolas", 9))
        self.sd_log.pack(fill="both", expand=True)

    def _find_plugin_yamls(self, folder: Path, *, limit: int = 5) -> list[Path]:
        """plugin.yaml 不一定放在第一層。上一版只看深度 0/1，於是 ANnoTation
        （18 個 plugin.yaml，全在更深的地方）被判定成「不是 CIM 模組」——
        對一個站在正確分頁上的人講的,而且他要的東西就在那裡。"""
        found: list[Path] = []
        for depth, pattern in enumerate(("plugin.yaml", "*/plugin.yaml",
                                         "*/*/plugin.yaml", "*/*/*/plugin.yaml")):
            try:
                for hit in folder.glob(pattern):
                    found.append(hit)
                    if len(found) >= limit:
                        return found
            except OSError:
                break
        return found

    def _wrong_tab_hint(self, folder: Path) -> str:
        """「找不到 plugin.yaml」對著一個 Streamlit 專案講是沒有意義的。

        判斷用的是「聞起來像不像 Streamlit 專案」，不是「能不能唯一決定入口檔」——
        後者會讓 ANnoTation 這種多候選的專案完全拿不到提示。反過來，若資料夾裡
        其實有 plugin.yaml（只是藏得比較深，或內容壞了），就不要把站對頁的人趕去別頁。"""
        try:
            folder = Path(folder)
            hits = self._find_plugin_yamls(folder)
            if hits:
                # 這個資料夾底下真的有模組。要嘛是「模組根目錄」指高了一層（那就告訴他
                # 該指哪裡），要嘛他已經指對了、錯的是別的東西（那就閉嘴，不要生一句
                # 「請把欄位改成它現在的值」——那是上一版真的會講出來的話）。
                roots = sorted({h.parent.parent for h in hits if h.parent.parent != folder})
                if not roots:
                    return ""              # 已經指在對的那一層；問題不在這裡
                return ("\n\n這個資料夾底下其實有 plugin.yaml"
                        f"（例：{hits[0].relative_to(folder)}），只是不在掃描的那一層。\n"
                        f"請把「模組根目錄」改指到：{roots[0]}\n"
                        "（模組根目錄 = 直接裝著各個模組資料夾的那一層。）")
            if not looks_like_streamlit(folder):
                return ""
            entry = find_entrypoint(folder)
        except OSError:
            return ""

        which = f"（入口：{entry.value.name}）" if entry.found else "（入口有多個候選，屆時可自行指定）"
        return (f"\n\n這看起來是一個 Streamlit 專案{which}，不是 CIM 平台模組"
                "（本頁需要 plugin.yaml）。\n"
                "請改用上方的「Streamlit 專案 → 桌面 App」分頁——"
                "它會把這個專案打成 User 可直接執行的資料夾。\n"
                f"（那一頁的「專案資料夾」填：{folder}）")

    def _detect_environment(self) -> None:
        """殼與 runtime 每次建置都一樣，是我們找得到的東西——別叫人來輸入。
        但偵測結果一定要顯示出來：靜靜地用一個猜到的路徑，比問還糟。"""
        shell = find_shell()
        runtime = find_runtime()
        if shell.found:
            self.sd_shell_var.set(str(shell.value))
        if runtime.found:
            self.sd_runtime_var.set(str(runtime.value))

        parts = [
            f"Tauri 殼 {'✓' if shell.found else '✗'}（{shell.source}）",
            f"可攜 Python {'✓' if runtime.found else '✗'}（{runtime.source}）",
        ]
        text = "建置環境：" + "　·　".join(parts)
        if not runtime.found:
            text += "\n" + runtime.hint
        elif not shell.found:
            text += "\n" + shell.hint
        self.sd_env_var.set(text)

        # 只有在真的缺 runtime 時才出現「下載」按鈕，不佔版面。
        if runtime.found:
            self.sd_fetch_button.pack_forget()
        else:
            self.sd_fetch_button.pack(side="left", padx=(12, 0))

    _FAT_OUTPUT = "dist/streamlit-apps"
    _STORE_OUTPUT = "dist/streamlit-store"

    def _on_store_mode_toggled(self) -> None:
        """兩種模式對「輸出位置」的意思不同：fat 模式是「放交付包的資料夾」，
        store 模式是「整棵樹的根」。把 store 樹建到裝著 fat 包的資料夾裡會讓人
        (合理地)以為壞掉了，所以切換模式時把預設輸出換掉，並明講差別。"""
        store = self.sd_store_var.get()
        current = Path(self.sd_output_var.get().strip())
        defaults = {(ROOT / self._FAT_OUTPUT).resolve(), (ROOT / self._STORE_OUTPUT).resolve()}
        if current.resolve() in defaults:      # 只在使用者沒自訂過時才動它
            self.sd_output_var.set(str(ROOT / (self._STORE_OUTPUT if store else self._FAT_OUTPUT)))
        self.sd_mode_hint.configure(
            text=("「輸出位置」= Store 樹的根（apps/ + deps/ + bootstrap/）。"
                  "同一版本號建過就不能再建（版本目錄不可變），改版請換版本號。"
                  if store else
                  "「輸出位置」= 放交付包的資料夾，每個包自帶完整 runtime。")
        )
        # 交付／維護只在 Store 佈局下有意義（fat 包沒有版本、沒有共用可回收）。
        if store:
            self.sd_source_row.pack(fill="x", pady=(6, 0), before=self.sd_actions)
            self.sd_deliver.pack(fill="x", pady=(10, 0), after=self.sd_actions)
        else:
            self.sd_source_row.pack_forget()
            self.sd_deliver.pack_forget()

    def _toggle_advanced(self) -> None:
        if self.sd_advanced_var.get():
            self.sd_advanced.pack(fill="x", pady=(10, 0), before=self.sd_actions)
        else:
            self.sd_advanced.pack_forget()

    def _start_fetch_runtime(self) -> None:
        """一次性取得可攜 CPython（沿用 nativeApp 既有的 fetch 腳本，不重造）。"""
        if self._worker and self._worker.is_alive():
            return
        script = ROOT.parent / "nativeApp" / "scripts" / "win" / "fetch-standalone-python.ps1"
        if not script.is_file():
            messagebox.showerror("找不到腳本", f"找不到:\n{script}\n\n請改用「進階設定」手動指定 runtime。")
            return
        dest = ROOT / ".runtime-cache" / "python311"
        self._set_desktop_busy(True)
        self.sd_status_var.set("下載可攜 Python…（需連網，只需做一次）")
        self._clear_desktop_log()

        def work() -> None:
            try:
                proc = subprocess.run(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script),
                     "-DestRoot", str(dest), "-Flatten"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                for line in (proc.stdout or "").splitlines():
                    self._events.put(("desktop_line", line))
                if proc.returncode != 0:
                    self._events.put(("desktop_error", f"下載失敗（exit {proc.returncode}）：{proc.stderr.strip()[:400]}"))
                    return
                self._events.put(("desktop_runtime_ready", dest))
            except OSError as exc:
                self._events.put(("desktop_error", f"下載失敗：{exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _browse_sd_project(self) -> None:
        value = filedialog.askdirectory(title="選擇 Streamlit 專案資料夾", initialdir=self.sd_project_var.get())
        if not value:
            return
        self._apply_project(Path(value))

    def _apply_project(self, project: Path) -> None:
        self.sd_project_var.set(str(project))
        entry = find_entrypoint(project)
        self.sd_entry_var.set(str(entry.value) if entry.found else "")
        self.sd_name_var.set(suggest_name(project))
        if entry.found:
            self.sd_status_var.set(f"入口檔案：{entry.source}。可以按「檢查專案」。")
        else:
            # 猜不到就說猜不到，不要塞一個假的預設值進去讓建置到一半才炸。
            self.sd_status_var.set(entry.hint or "找不到入口檔案，請手動指定。")

    def _browse_sd_entry(self) -> None:
        value = filedialog.askopenfilename(
            title="選擇 Streamlit 入口檔（.py）",
            initialdir=self.sd_project_var.get(),
            filetypes=[("Python", "*.py")],
        )
        if value:
            self.sd_entry_var.set(value)

    def _browse_sd_shell(self) -> None:
        value = filedialog.askopenfilename(title="選擇預建 Tauri 殼（cim-light.exe）",
                                           filetypes=[("Executable", "*.exe")])
        if value:
            self.sd_shell_var.set(value)

    def _browse_sd_runtime(self) -> None:
        value = filedialog.askdirectory(title="選擇可攜 Python runtime（含 python.exe）",
                                        initialdir=self.sd_runtime_var.get())
        if value:
            self.sd_runtime_var.set(value)

    def _browse_sd_output(self) -> None:
        value = filedialog.askdirectory(title="選擇輸出位置", initialdir=self.sd_output_var.get())
        if value:
            self.sd_output_var.set(value)

    def _browse_webview2(self) -> None:
        value = filedialog.askopenfilename(
            title="選擇 WebView2 離線安裝檔（MicrosoftEdgeWebView2RuntimeInstaller*.exe）",
            filetypes=[("Executable", "*.exe"), ("All", "*.*")])
        if value:
            self.sd_webview2_var.set(value)

    def _refresh_deliver_versions(self) -> None:
        """把這棵樹上「裝得起來」的版本列出來，預設選最新的那一版。

        預設值很重要:管理員按「匯出更新包」時心裡想的是「發最新的」，而 state 的
        current 在建置機上是「上一版」（新版是 pending，因為建置機自己不會去啟動它）。
        預設給 current 就會發出產線已經在跑的那一版。"""
        self.sd_deliver_version.configure(values=())
        self.sd_deliver_version_hint.configure(text="")
        if not (self._sd_last_store and self._sd_last_app):
            return
        try:
            versions = list_store_versions(self._sd_last_store, self._sd_last_app)
        except Exception as exc:  # GUI 邊界
            self.sd_deliver_version_hint.configure(text=f"讀不到版本清單:{exc}")
            return
        installable = [v for v in versions if getattr(v, "is_complete", True)]
        labels = [v.version for v in installable]
        self.sd_deliver_version.configure(values=labels)
        if not labels:
            self.sd_deliver_version_hint.configure(text="這個 App 還沒有任何完整的版本。")
            return
        # 預設 = 最新的完整版本。不是 state.current(那是產線已經在跑的那一版),
        # 也不是「這個 session 剛好碰過的那一版」。
        try:
            default = newest_store_version(self._sd_last_store, self._sd_last_app)
        except Exception:
            default = None
        chosen = default if default in labels else labels[0]
        self.sd_deliver_version_var.set(chosen)
        roles = {v.version: (getattr(v, "role", "") or "") for v in installable}
        role_text = {"current": "產線目前跑的版本", "pending": "已建好、待套用",
                     "previous": "上一版", "last_known_good": "最後確認可用"}
        described = "、".join(
            f"{v}（{role_text.get(roles[v], '未使用')}）" for v in labels[:4])
        self.sd_deliver_version_hint.configure(text=f"這棵樹上:{described}")

    def _browse_sd_lock(self) -> None:
        value = filedialog.askopenfilename(
            title="選擇相依 lock 檔（requirements.lock.txt）",
            initialdir=self.sd_project_var.get(),
            filetypes=[("Requirements", "*.txt"), ("All", "*.*")])
        if value:
            self.sd_lock_var.set(value)

    def _desktop_request(self) -> BuildRequest:
        # 空字串會被 Path("") 解析成「打包器自己的 repo」（cwd），於是「檢查專案」
        # 會去驗 native_Provision 自己，還叫使用者去改我們的 pyproject.toml。
        # 這不是一個路徑錯誤，是一個「你還沒選專案」——就這樣講。
        project = self.sd_project_var.get().strip()
        if not project:
            raise ValueError("請先選擇 Streamlit 專案資料夾（按「瀏覽…」）。")
        entry = self.sd_entry_var.get().strip()
        if not entry:
            raise ValueError("找不到入口檔案。請在「入口檔案」按「瀏覽…」指定要跑的 .py。")
        try:
            port = int(self.sd_port_var.get().strip() or 0)   # 0 = 隨機挑可用埠
        except ValueError as exc:
            raise ValueError(f"偏好連接埠不是數字：{self.sd_port_var.get()}") from exc
        lock = self.sd_lock_var.get().strip()
        extra = tuple(p.strip() for p in self.sd_exclude_var.get().split(";") if p.strip())
        extras = tuple(e.strip() for e in self.sd_extras_var.get().split(",") if e.strip())
        webview2 = self.sd_webview2_var.get().strip()
        app_id = self.sd_app_id_var.get().strip()
        return BuildRequest(
            app_id_override=app_id or None,
            project_dir=Path(project),
            entrypoint=Path(entry),
            display_name=self.sd_name_var.get().strip(),
            output_dir=Path(self.sd_output_var.get().strip()),
            shell_exe=Path(self.sd_shell_var.get().strip()),
            runtime_template=Path(self.sd_runtime_var.get().strip()),
            preferred_port=port,
            requirements=Path(lock) if lock else None,
            extra_excludes=extra,
            extras=extras,
            webview2_installer=Path(webview2) if webview2 else None,
        )

    def _set_desktop_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.sd_check_button.configure(state=state)
        self.sd_build_button.configure(state=state)
        self.sd_cancel_button.configure(state="normal" if busy else "disabled")
        if hasattr(self, "sd_fetch_button"):
            self.sd_fetch_button.configure(state=state)
        have_store = self._sd_last_store is not None and not busy
        self.sd_export_full_button.configure(state="normal" if have_store else "disabled")
        self.sd_export_update_button.configure(state="normal" if have_store else "disabled")
        self.sd_gc_button.configure(state="normal" if have_store else "disabled")
        if busy:
            self.sd_progress.start(12)
        else:
            self.sd_progress.stop()

    def _cancel_desktop(self) -> None:
        """真的取消：設 event → builder 在階段邊界看到 → taskkill 掉整棵 pip 程序樹
        → 刪掉 staging → 回傳 cancelled=True → 這裡跳「已取消」而不是「建立完成」。

        （上一版這個鈕只寫了一個沒有人讀的旗標，pip 照跑到底，最後還跳「建立完成」。
        一個宣稱會停、實際不會停、還回報你它停了的按鈕，比沒有按鈕更糟。）"""
        if not (self._worker and self._worker.is_alive()):
            return
        if not messagebox.askyesno(
                "取消建置",
                "要中止目前的建置嗎？\n\n"
                "正在下載／安裝的 pip 會被中止，暫存目錄（.staging-*）會清乾淨，\n"
                "不會留下半成品，也不會產生可交付資料夾。"):
            return
        self._sd_cancel.set()
        self._append_desktop_log("已要求取消；正在中止 pip 並清理暫存…")
        self.sd_status_var.set("正在取消…")
        self.sd_cancel_button.configure(state="disabled")

    # ── 交付與維護 ────────────────────────────────────────────────────────────

    def _browse_update_source(self) -> None:
        value = filedialog.askdirectory(title="選擇更新來源資料夾（USB／網路磁碟）")
        if value:
            self.sd_update_source_var.set(value)

    @staticmethod
    def _disk_story(plan) -> str:
        """「沒有可回收的項目」對一個 C 槽快滿的人來說,正確但無用。

        他要的是「空間到底去哪了」。GC 現在量得出來:磁碟剩多少、這棵樹佔多少、
        最大的幾個吃空間的東西是什麼(版本 / log / cache / runtime)——
        而且如果問題不在這棵樹上,就直接說「不是它吃掉的,請往別處找」。"""
        lines: list[str] = []
        disk = getattr(plan, "disk", None)
        if disk is not None and getattr(disk, "known", False):
            lines.append(f"{disk.label}:剩 {disk.free_mb / 1024:.1f} GB / 共 "
                         f"{disk.total_mb / 1024:.1f} GB（{disk.free_pct:.0f}% 可用）")
        if hasattr(plan, "store_mb"):
            lines.append(f"這棵 Store 樹佔:{plan.store_mb():.0f} MB")
        if hasattr(plan, "store_is_the_problem") and not plan.store_is_the_problem():
            lines.append("→ C 槽的空間不是被這棵樹吃掉的,請往別處找"
                         "（下載資料夾、其他程式的快取、Windows 更新暫存…）。")
        elif hasattr(plan, "biggest"):
            biggest = plan.biggest(3)
            if biggest:
                lines.append("最大的幾項:")
                lines += [f"　·　{c.label}:{c.mb:.0f} MB"
                          + ("（可回收）" if getattr(c, "reclaimable_mb", 0) > 1 else "")
                          for c in biggest]
        return "\n".join(lines) if lines else "（量不到磁碟資訊。）"

    @staticmethod
    def _cancel_message(result) -> str:
        """取消之後,講「真的發生了什麼」——builder 已經查證過了,不要覆蓋它。

        builder 在 rmtree 之後會回頭確認 staging 到底還在不在(剛殺完 pip 程序樹,
        Windows 常常還鎖著那些檔案),把答案放進 result.staging_left:
        None = 真的刪掉了;有值 = 那個目錄還躺在磁碟上。

        判斷要看這個欄位,不要去解析訊息字串——GUI 曾經無條件蓋成
        「暫存目錄已清乾淨」,對著一個還躺著 600 MB 的磁碟。"""
        told = (getattr(result, "message", "") or "").strip()
        left = getattr(result, "staging_left", None)
        if left:
            return (told or "已取消建置。") + (
                f"\n\n暫存目錄還在磁碟上（有檔案被系統鎖住,暫時刪不掉）:\n{left}\n"
                "下次建置時會自動清掉;要立刻回收空間,關掉防毒掃描後手動刪除即可。")
        return told or "已取消建置,暫存目錄已清乾淨。"

    def _open_existing_store(self) -> None:
        """讓「發一版出去」與「清磁碟」不再綁在「這個 session 剛建過」上。

        管理員星期一早上打開工具,想把上週建好的 v1.2.0 發給產線——以前三個按鈕
        全是灰的,唯一的解法是「再建一次」,而那正是他不想做的事(而且同一個版本號
        還會被版本目錄不可變的規則擋下來)。"""
        value = filedialog.askdirectory(title="選擇既有的 Store 樹根目錄（裡面有 apps\\ 與 deps\\）",
                                        initialdir=self.sd_output_var.get())
        if not value:
            return
        root = Path(value)
        try:
            apps = list_store_apps(root)
        except Exception as exc:  # GUI 邊界
            messagebox.showerror("讀不到這棵 Store 樹", f"{exc}\n\n{root}")
            return
        if not apps:
            messagebox.showerror(
                "這不是一棵 Store 樹",
                f"{root}\n\n找不到 apps\\<app>\\state\\state.json。\n"
                "請選「輸出位置」指到的那個根目錄（裡面應該有 apps\\、deps\\、bootstrap\\）。")
            return

        if len(apps) == 1:
            chosen = apps[0]
        else:
            names = "\n".join(f"  · {a['app_id']}（目前 {a['current'] or '未設定'}）" for a in apps)
            answer = simpledialog.askstring(
                "這棵樹上有多個 App",
                f"要操作哪一個?請輸入 app id:\n\n{names}",
                initialvalue=apps[0]["app_id"], parent=self)
            if not answer:
                return
            match = [a for a in apps if a["app_id"] == answer.strip()]
            if not match:
                messagebox.showerror("找不到這個 App", f"{answer} 不在這棵樹上。")
                return
            chosen = match[0]

        self._sd_last_store = root
        self._sd_last_app = chosen["app_id"]
        # 匯出的預設對象是「目前版本」——那才是產線上正在跑的東西。
        # 預設值交給 _refresh_deliver_versions()（它挑最新的完整版本）；current 只是
        # 「產線目前跑的」，拿它當匯出預設值，發出去的就是他們已經有的那一版。
        self._sd_last_version = chosen["pending"] or chosen["current"]
        self._sd_last_package = root
        self.sd_store_var.set(True)
        self._on_store_mode_toggled()
        self.sd_output_var.set(str(root))
        self.sd_export_full_button.configure(state="normal")
        self.sd_export_update_button.configure(state="normal")
        self.sd_gc_button.configure(state="normal")
        self.sd_open_button.configure(state="normal")
        summary = "、".join(
            f"{a['app_id']}(目前 {a['current'] or '未設定'}"
            + (f",待套用 {a['pending']}" if a["pending"] else "") + ")"
            for a in apps)
        self._refresh_deliver_versions()
        self.sd_store_info.configure(
            text=f"已開啟 Store 樹:{root}\n"
                 f"這棵樹上的 App:{summary}\n"
                 f"接下來的匯出／回收都會針對「{self._sd_last_app} {self._sd_last_version}」。")
        self.sd_status_var.set(f"已開啟既有 Store 樹({self._sd_last_app} {self._sd_last_version})。")

    def _export_store(self, *, full: bool) -> None:
        """完整交付 = 整棵可執行的樹；更新包 = 只有這一版，要靠 --install 或更新來源套用。

        上一版兩者都走 export_update()，於是「首次部署」匯出的東西沒有 bootstrap/、
        沒有 start.bat、沒有 state/——目標機拿到一個永遠打不開的資料夾。"""
        if not (self._sd_last_store and self._sd_last_app):
            return

        # 這棵樹上不只一個 App 時,「完整交付」要問清楚:只給這一個,還是整棵樹?
        # export_full_tree(app_id=None) 一直做得對,只是 GUI 從來沒有地方能按到它。
        whole_tree = False
        if full:
            try:
                apps = list_store_apps(self._sd_last_store)
            except Exception:
                apps = []
            if len(apps) > 1:
                names = "、".join(a["app_id"] for a in apps)
                answer = messagebox.askyesnocancel(
                    "這棵樹上有多個 App",
                    f"這棵 Store 樹上有 {len(apps)} 個 App：{names}\n\n"
                    "【是】整棵樹一起交付（目標機一次拿到全部，共用同一份 runtime）\n"
                    f"【否】只交付「{self._sd_last_app}」這一個\n"
                    "【取消】不匯出")
                if answer is None:
                    return
                whole_tree = bool(answer)

        # 使用者在下拉裡選的那一版才算數（預設是最新的完整版本，不是 state.current）。
        chosen = self.sd_deliver_version_var.get().strip() or self._sd_last_version
        if not chosen:
            messagebox.showerror("還沒有可交付的版本", "這棵樹上找不到任何完整的版本。")
            return
        self._sd_last_version = chosen

        # 更新包預設不含 runtime(那正是它只有十幾 MB 的原因)。但如果這一版換了
        # Python 相依或換了殼,不帶 runtime 的包在目標機上是裝不起來的。與其讓
        # export_update() 丟例外、管理員盯著一個看不懂的錯誤,不如先問清楚。
        include_runtime = False
        if not full:
            try:
                needs = update_needs_runtime(self._sd_last_store, self._sd_last_app,
                                             self._sd_last_version)
            except Exception:                       # 讀不到就當作不需要,讓匯出自己去擋
                needs = False
            if needs:
                if not messagebox.askyesno(
                        "這一版換了相依,更新包必須包含 runtime",
                        f"版本 {self._sd_last_version} 的 Python 相依（或 Tauri 殼）"
                        "跟上一版不一樣。\n\n"
                        "只帶版本的增量包在目標機上會裝不起來——它會去找一份那台機器\n"
                        "沒有的 runtime。\n\n"
                        "要改成「包含 runtime」的更新包嗎？（會大很多，約 500 MB）\n"
                        "選「否」則不匯出。"):
                    self.sd_status_var.set("已取消匯出。")
                    return
                include_runtime = True

        out = filedialog.askdirectory(title="匯出到哪裡（USB／交付資料夾）")
        if not out:
            return

        self._set_desktop_busy(True)
        self.sd_status_var.set("匯出中…")
        store, app_id, version = self._sd_last_store, self._sd_last_app, self._sd_last_version

        say = lambda line: self._events.put(("desktop_line", line))   # noqa: E731

        def work() -> None:
            try:
                if full:
                    # version= 一定要傳。少了它,export_full_tree 會回頭去交付
                    # state.current——也就是產線「已經在跑」的那一版。管理員在下拉裡
                    # 選了 v1.1.0、按下匯出、走一趟工廠,裝上去的還是 v1.0.0。
                    # (而且工具自己的警告會叫他「請改指定版本」,但 GUI 裡唯一能指定的
                    #  地方,就是這個被忽略掉的下拉。)
                    result = export_streamlit_full(
                        store, Path(out),
                        app_id=None if whole_tree else app_id,
                        version=None if whole_tree else version,
                        progress=say)
                else:
                    # include_runtime 是上面問過使用者的答案。這裡曾經硬寫 False，
                    # 於是「這一版換了相依，要含 runtime 嗎？」問了、答了、然後被忽略——
                    # 產出的包在目標機一樣裝不起來。
                    result = export_streamlit_update(store, app_id, version, Path(out),
                                                     include_runtime=include_runtime,
                                                     progress=say)
                self._events.put(("desktop_export_done", (result, full)))
            except Exception as exc:  # GUI 邊界
                self._events.put(("desktop_error", f"匯出失敗：{exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _gc_store(self) -> None:
        if not self._sd_last_store:
            return
        self._clear_desktop_log()
        try:
            plan = streamlit_gc(self._sd_last_store, apply=False,
                                log=lambda line: self._append_desktop_log(str(line)))
        except Exception as exc:  # GUI 邊界
            messagebox.showerror("回收失敗", str(exc))
            return
        reclaimable = plan.reclaimable_mb()
        # 「有沒有東西可回收」要問計畫本身,不要用 MB 數去猜。
        # reclaimable_mb() 是一個「量測」——而 _mb() 整個包在 try/except 裡:
        # 只要有一個檔案讀不到(被執行中的 App 鎖住,這是常態),整份 450 MB 的
        # runtime 就會回報成 0 MB,於是我們對著一個滿的磁碟說「沒有可回收的項目」。
        empty = plan.nothing_to_reclaim() if hasattr(plan, "nothing_to_reclaim") \
            else (not plan.delete_versions and not plan.delete_runtimes
                  and not plan.delete_shells)
        if empty:
            note = ""
            if getattr(plan, "self_hosted", None):
                # 「沒有東西可回收」跟「唯一能回收的那份,正是我腳下這一份」是兩件事。
                note = ("\n\n不過:GC 正在用 " + str(plan.self_hosted) +
                        " 這份 runtime 執行,\n所以它自己即使沒被引用也不會被刪除。\n"
                        "要回收它,請改用另一份 runtime 的 python.exe 重跑一次。")
            # 對一個「C 槽快滿」的人說「沒有可回收的項目」然後閉嘴,是正確而無用的。
            # 他來這裡不是想聽「沒事」,是想知道空間到底去哪了。
            note += "\n\n" + self._disk_story(plan)
            messagebox.showinfo("沒有可回收的項目",
                                "這棵樹裡沒有任何「沒被引用」的版本或 runtime。" + note)
            return
        if not messagebox.askyesno(
                "確認回收",
                f"可回收 {reclaimable:.0f} MB（詳見紀錄區）。\n\n"
                "只會刪除「沒有任何版本槽、也沒有執行中實例」引用的項目。\n確定要刪除嗎？"):
            return

        # 報「實際刪掉了什麼」,不是報「本來打算刪什麼」。上一版把 apply 的回傳值
        # 直接丟掉,拿 dry-run 的預測值去跳「已回收約 480 MB」——即使一個檔案都沒刪掉
        # (檔案被 App 佔用是最常見的情況,而那正是現場 IT 最需要知道的事)。
        try:
            done = streamlit_gc(self._sd_last_store, apply=True,
                                log=lambda line: self._append_desktop_log(str(line)))
        except Exception as exc:  # GUI 邊界
            messagebox.showerror("回收失敗", str(exc))
            return

        freed = done.reclaimed_mb() if hasattr(done, "reclaimed_mb") else 0.0
        failures = list(getattr(done, "failures", ()) or ())
        if failures:
            detail = "\n".join(f"　·　{f}" for f in failures[:5])
            messagebox.showwarning(
                "部分回收",
                f"實際回收了 {freed:.0f} MB（本來預估 {reclaimable:.0f} MB）。\n\n"
                f"這些刪不掉,空間沒有回收:\n{detail}\n\n"
                "最常見的原因是 App 正在執行中佔住檔案。\n"
                "請關掉所有用到這棵樹的 App,再回收一次。")
        elif freed < 1:
            messagebox.showwarning(
                "沒有回收到空間",
                "雖然列出了可回收的項目，但實際上一個位元組都沒有釋放。\n"
                "請查看紀錄區的訊息。")
        else:
            messagebox.showinfo("回收完成", f"已回收 {freed:.0f} MB。")

    def _start_desktop_check(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._clear_desktop_log()
        try:
            request = self._desktop_request()
        except ValueError as exc:
            self._events.put(("desktop_error", str(exc)))
            return
        store_mode = self.sd_store_var.get()
        try:
            if store_mode:
                errors = validate_streamlit_store_request(
                    request, self.sd_version_var.get().strip(),
                    Path(self.sd_output_var.get().strip()))
            else:
                errors = validate_streamlit_request(request)
        except Exception as exc:  # 唯讀磁碟等；Tk callback 裡的例外會靜靜消失
            messagebox.showerror("檢查失敗", str(exc))
            return

        if errors:
            self.sd_status_var.set("檢查未通過。")
            for error in errors:
                self._append_desktop_log("✗ " + error)
            messagebox.showerror("檢查未通過", "\n\n".join(errors))
            return

        self.sd_status_var.set("檢查通過，可以建立。")
        self._append_desktop_log(f"✓ 專案：{request.project_dir}")
        self._append_desktop_log(f"✓ 入口：{request.entrypoint}")
        self._append_desktop_log(f"✓ 應用 ID：{request.app_id}")

        # 「還不確定，但你該知道」的事：requirements.txt/pyproject 只宣告直接相依，
        # 所以 numpy 沒被列出來不代表裝不到（pandas 會帶進來）。這種事不該擋建置，
        # 但沉默也不對——安裝完成後我們會再驗一次，真的缺才擋。
        try:
            soft = streamlit_warnings_for(request)
        except Exception:                     # 檢查的附加資訊失敗，不該讓檢查失敗
            soft = []
        # WebView2 是無網工廠機唯一「裝不起來就開不了」的相依,而離線產線正是這個
        # 產品存在的理由。這件事要在「還來得及、還很便宜」的時候講——0 秒、建置前,
        # 而不是等他建完 600MB、走到工廠、才發現自救的那條路是死的。
        if not request.webview2_installer:
            soft = list(soft) + [
                "沒有指定 WebView2 離線安裝檔。目標機若不能上網、又沒裝過 WebView2,"
                "App 會開不起來(而且當場沒辦法裝)。"
                "請在「進階設定 → WebView2 離線安裝檔」指定它。"
                "（已經建好的包也不必重建:把安裝檔複製到包裡的 prereq\\ 底下就行。）"]
        for warning in soft:
            self._append_desktop_log("⚠ " + warning)

        # 真實預估，不是寫死的「450–550 MB」：實際掃專案 + 判斷 runtime 會不會重用。
        preview = scan_streamlit_project(request)
        if store_mode:
            root = Path(self.sd_output_var.get().strip())
            version = self.sd_version_var.get().strip()
            reuse = runtime_would_be_reused(request, root)
            self._append_desktop_log(
                f"✓ 輸出：{root / 'apps' / request.app_id / 'versions' / version}")
            self._append_desktop_log(
                f"　 本次預估新增：{preview.application_mb:.0f} MB（你的專案）"
                + ("　+ 0 MB（runtime 重用既有的）" if reuse
                   else "　+ 約 500–750 MB（要新建一份 runtime）"))
        else:
            self._append_desktop_log(f"✓ 輸出：{request.package_dir}")
            self._append_desktop_log(
                f"　 預估大小：你的專案 {preview.application_mb:.0f} MB"
                " + runtime 約 500–750 MB（Streamlit 的相依很大，與你的 app 無關）")
        for note in getattr(preview, "notes", ()):      # 已自動排除:純資訊,不是警告
            self._append_desktop_log("· " + note)
        for warning in preview.warnings:
            self._append_desktop_log("⚠ " + warning)

    def _start_desktop_build(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._clear_desktop_log()
        try:
            request = self._desktop_request()
        except ValueError as exc:
            self._events.put(("desktop_error", str(exc)))
            return

        # 大檔警告要在「開工前」問，不是在六分鐘之後才在紀錄區飄過去——那時候
        # 東西已經複製完了，問了也無從回答。
        try:
            preview = scan_streamlit_project(request)
        except Exception:                      # 掃描失敗不該擋住建置
            preview = None
        # 只有「需要你決定」的事才擋人。已經自動排除掉的東西(wheels/、.git/)是
        # 好消息,不該長成一個確認框問你要不要排除它們——那是在問一個已經回答完的問題。
        if preview is not None and preview.warnings:
            body = "\n".join("・" + w for w in preview.warnings)
            if not messagebox.askokcancel(
                    "這個專案裡有一些大東西",
                    f"{body}\n\n"
                    f"預估你的專案會佔 {preview.application_mb:.0f} MB。\n"
                    "不想帶的東西可以在「進階設定 → 額外排除」填樣式（分號分隔，"
                    "例：data/*;*.mp4），或在專案根目錄放 .provisionignore。\n\n"
                    "要照現在的內容繼續建置嗎？"):
                self.sd_status_var.set("已取消。可先設定排除樣式再建置。")
                return

        self._sd_cancel.clear()
        self._set_desktop_busy(True)
        self.sd_status_var.set("建置中…（複製 runtime 與安裝相依需要數分鐘；可按「取消」中止）")
        store_mode = self.sd_store_var.get()
        store_version = self.sd_version_var.get().strip()
        store_root = Path(self.sd_output_var.get().strip())

        update_source = self.sd_update_source_var.get().strip() or None
        should_cancel = self._sd_cancel.is_set

        def work() -> None:
            try:
                if store_mode:
                    result = build_streamlit_store(
                        request, store_root, version=store_version,
                        update_source=update_source, should_cancel=should_cancel,
                        progress=lambda line: self._events.put(("desktop_line", line)))
                    self._events.put(("desktop_store_done", result))
                else:
                    result = build_streamlit_desktop(
                        request, should_cancel=should_cancel,
                        progress=lambda line: self._events.put(("desktop_line", line)))
                    self._events.put(("desktop_done", result))
            except Exception as exc:  # GUI 邊界：任何例外都要看得見
                self._events.put(("desktop_error", f"建置失敗：{exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _append_desktop_log(self, line: str) -> None:
        self.sd_log.configure(state="normal")
        self.sd_log.insert("end", line + "\n")
        self.sd_log.see("end")
        self.sd_log.configure(state="disabled")

    def _clear_desktop_log(self) -> None:
        self.sd_log.configure(state="normal")
        self.sd_log.delete("1.0", "end")
        self.sd_log.configure(state="disabled")

    def _open_sd_output(self) -> None:
        # 剛做好的那個包，不是它的上層資料夾——上層裡通常還躺著 apps/、deps/、
        # 別的 App 的包，開起來還要自己找哪一個才是。
        target = self._sd_last_package or self._sd_last_store or Path(self.sd_output_var.get())
        target = Path(target).resolve()
        if not target.exists():
            messagebox.showwarning("找不到輸出", f"資料夾不存在：\n{target}")
            return
        if os.name == "nt":
            # /select 會開啟上層並「反白」它，比單純開進去更容易辨認是哪一個。
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:  # pragma: no cover
            subprocess.Popen(["xdg-open", str(target)])

    def _browse_project(self) -> None:
        value = filedialog.askdirectory(title="選擇 CIM 平台專案", initialdir=self.project_var.get())
        if value:
            self.project_var.set(value)

    def _browse_dest(self) -> None:
        value = filedialog.askdirectory(title="選擇輸出位置", initialdir=str(Path(self.dest_var.get()).parent))
        if value:
            self.dest_var.set(value)

    def _browse_validation(self) -> None:
        value = filedialog.askdirectory(title="選擇隔離驗證資料夾", initialdir=str(Path(self.validation_var.get()).parent))
        if value:
            self.validation_var.set(value)

    def _browse_module_root(self) -> None:
        value = filedialog.askdirectory(title="選擇單一 Module 或 Modules 根資料夾", initialdir=self.module_root_var.get())
        if value:
            self.module_root_var.set(value)

    def _set_busy(self, busy: bool) -> None:
        self.scan_button.configure(state="disabled" if busy else "normal")
        self.build_button.configure(state="disabled" if busy or not self._tool_vars else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        has_output = ((Path(self.dest_var.get()) / "provision.json").is_file()
                      or (Path(self.dest_var.get()) / "source-packages").is_dir())
        self.validate_button.configure(state="disabled" if busy or not has_output else "normal")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _start_scan(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._set_busy(True)
        self.cancel_button.configure(state="disabled")  # 掃描很短，且不是可終止的 build 子程序
        self.status_var.set("正在掃描 plugin.yaml…")
        project = Path(self.project_var.get().strip())
        module_root = Path(self.module_root_var.get().strip())

        def work() -> None:
            try:
                gateway = PlatformGateway(project)
                result = discover_source_modules(module_root, gateway.python_cmd)
                self._events.put(("scan_ok", result))
            except Exception as exc:  # GUI 邊界需顯示可行動錯誤
                # 救援要看「兩個欄位」。上一版只看 module_root：一個把 Streamlit 專案
                # 填進「平台專案」、module_root 還留著預設值的人，會撞到一個關於
                # nativeApp 的錯誤，而畫面上沒有任何一個字提到他的專案。
                hint = self._wrong_tab_hint(module_root) or self._wrong_tab_hint(project)
                self._events.put(("error", f"掃描失敗：{exc}{hint}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _render_tools(self, result) -> None:
        for widget in self.tool_list.winfo_children():
            widget.destroy()
        self._tool_vars.clear()
        self._tool_requires.clear()
        self._source_modules = {m.tool_id: m for m in result}
        enabled = [m for m in result if m.enabled]
        for tool in result:
            var = tk.BooleanVar(value=tool.enabled)
            self._tool_vars[tool.tool_id] = var
            self._tool_requires[tool.tool_id] = list(tool.requires)
            dep = f"{len(tool.requires)} 個相依" if tool.requires else "無額外相依"
            state = "normal" if tool.enabled else "disabled"
            text = f"{tool.tool_id}    {tool.version}    {dep}    {tool.name}"
            ttk.Checkbutton(self.tool_list, text=text, variable=var, state=state).pack(anchor="w", fill="x", pady=2)
        if not result:
            ttk.Label(self.tool_list, text="沒有啟用且宣告 requires: 的工具。", foreground="#666").pack(anchor="w", pady=8)
        self.status_var.set(f"找到 {len(result)} 個 Module；{len(enabled)} 個可選。原始碼與相依將分開輸出。")
        self.build_button.configure(state="normal" if enabled else "disabled")

    def _select_all(self, value: bool) -> None:
        for var in self._tool_vars.values():
            var.set(value)

    def _start_build(self) -> None:
        selected = tuple(tool_id for tool_id, var in self._tool_vars.items() if var.get())
        if not selected:
            messagebox.showwarning("尚未選擇工具", "請至少選擇一個需要打包的工具。")
            return
        project = Path(self.project_var.get().strip())
        dest = Path(self.dest_var.get().strip())
        if dest.resolve() == project.resolve() or project.resolve() in dest.resolve().parents:
            if not messagebox.askyesno("輸出位於專案內", "輸出位置位於平台專案內，可能產生大量檔案。仍要繼續嗎？"):
                return

        self._clear_log()
        self._append_log(f"準備打包：{', '.join(selected)}")
        self.status_var.set("正在建立離線工具包；大型套件可能需要數分鐘…")
        self.open_button.configure(state="disabled")
        self._set_busy(True)
        self._active_process = self._build
        modules = tuple(self._source_modules[t] for t in selected)
        options = BuildOptions(project, dest, selected, force=self.force_var.get(),
                               threshold_mb=DEFAULT_BIG_THRESHOLD_MB, source_modules=modules,
                               launch_mode=self.launch_mode_var.get())

        def work() -> None:
            try:
                code = self._build.run(options, lambda line: self._events.put(("line", line)))
                self._events.put(("build_done", (code, dest, self._build.cancelled)))
            except Exception as exc:
                self._events.put(("error", f"無法啟動建置：{exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _cancel(self) -> None:
        self.status_var.set("正在取消…")
        self._active_process.cancel()

    def _start_validation(self) -> None:
        tool_id = self.validation_tool_var.get().strip()
        if not tool_id:
            messagebox.showwarning("尚未選擇工具", "請選擇要透過 Tauri 實際啟動的工具。")
            return
        validation_dir = Path(self.validation_var.get().strip())
        if not messagebox.askyesno(
            "開始實機驗證",
            f"驗證資料夾內既有的 cache、venv、logs 與 WebView2 測試資料會被重建：\n\n{validation_dir}\n\n是否繼續？",
        ):
            return
        options = ValidationOptions(Path(self.dest_var.get()), validation_dir, Path(self.project_var.get()), tool_id)
        self._append_log(f"開始實機驗證 {tool_id}：Apply → Warmup → Tauri")
        self.status_var.set("正在套用補給包並執行 Tauri 實機驗證…")
        self._active_process = self._validation
        self._set_busy(True)

        def work() -> None:
            try:
                code = self._validation.run(options, lambda line: self._events.put(("line", line)))
                self._events.put(("validation_done", (code, validation_dir, self._validation.cancelled)))
            except Exception as exc:
                self._events.put(("error", f"無法啟動驗證：{exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "line":
                    self._append_log(str(payload))
                elif kind == "scan_ok":
                    self._set_busy(False)
                    self._render_tools(payload)
                elif kind == "error":
                    self._set_busy(False)
                    self.status_var.set(str(payload))
                    self._append_log(str(payload))
                    messagebox.showerror("操作失敗", str(payload))
                elif kind == "build_done":
                    code, dest, cancelled = payload
                    self._set_busy(False)
                    if cancelled:
                        self.status_var.set("已取消。可重新打包；核心會安全重建未完成的工具包。")
                    elif code == 0:
                        self._show_success(Path(dest))
                    else:
                        self.status_var.set("打包未完成。請查看下方紀錄中的失敗工具與原因。")
                        messagebox.showerror("打包失敗", "部分或全部工具打包失敗，請查看建置紀錄。")
                elif kind == "desktop_line":
                    self._append_desktop_log(str(payload))
                elif kind == "desktop_runtime_ready":
                    self._set_desktop_busy(False)
                    self._detect_environment()          # 重新偵測，按鈕自己消失
                    self.sd_status_var.set("可攜 Python 已就緒，可以建立交付資料夾了。")
                    messagebox.showinfo("下載完成", f"可攜 Python runtime 已就緒：\n{payload}")
                elif kind == "desktop_error":
                    self._set_desktop_busy(False)
                    self.sd_status_var.set(str(payload))
                    self._append_desktop_log("✗ " + str(payload))
                    messagebox.showerror("建置失敗", str(payload))
                elif kind == "desktop_export_done":
                    self._set_desktop_busy(False)
                    result, full = payload
                    target = result.out_dir
                    kind_text = ("完整交付（整棵樹，拿到就能跑）" if full
                                 else "更新包（只有這一版）")
                    self.sd_status_var.set(f"已匯出：{target}")
                    self._append_desktop_log(f"✓ 匯出 {kind_text}：{target}（{result.total_mb:.0f} MB）")
                    if full:
                        # 交付包裡「真的存在」的那個啟動檔。多 App 的樹沒有 start.bat
                        # （它會被換成 start-<app>.bat），寫死「雙擊 start.bat」等於
                        # 叫產線去點一個不存在的檔案。
                        entry = getattr(result, "entry_hint", lambda: "")() or "雙擊 start.bat"
                        steps = ("把整個資料夾複製到目標機（或直接放 USB 上跑）。\n\n"
                                 "目標機的人要做的事：\n"
                                 f"  1. {entry}\n"
                                 "  2. 視窗出現後，在上方「工作流程」下拉確認選到這個 App\n"
                                 "  3. 按一次旁邊的「Start」按鈕\n\n"
                                 "第一次啟動會逐檔驗證共用 runtime（幾十秒），通過後才會啟用。")
                    else:
                        steps = ("這個包只能給「已經有這棵 Store 樹」的機器。兩種套用方式：\n\n"
                                 "  A. 放進更新來源目錄 → 那些機器下次啟動時自動更新。\n"
                                 "  B. 複製到目標機任一位置 → 執行 tools\\admin.bat →\n"
                                 "     選「套用已複製進來的更新包」→ 指到這個資料夾。\n\n"
                                 "（套用時會逐檔驗證；驗證不過就不會安裝，也不會動到目前版本。）")
                    # 匯出物自己會回報它知道的問題（最重要的是：沒附 WebView2 離線
                    # 安裝檔，而目標機是無網工廠機——那台機器會 exit 5 開不起來，
                    # 且沒有網路裝不了）。這種事必須在管理員「走去工廠之前」講。
                    export_warnings = list(getattr(result, "warnings", ()) or ())
                    for warning in export_warnings:
                        self._append_desktop_log("⚠ " + warning)
                    body = (f"{kind_text} 已匯出到：\n{target}\n"
                            f"大小 {result.total_mb:.0f} MB\n\n{steps}")
                    if export_warnings:
                        messagebox.showwarning(
                            "匯出完成（有需要注意的事）",
                            body + "\n\n⚠ " + "\n⚠ ".join(export_warnings))
                    else:
                        messagebox.showinfo("匯出完成", body)
                elif kind == "desktop_store_done":
                    self._set_desktop_busy(False)
                    result = payload
                    if getattr(result, "cancelled", False):
                        # builder 已經「驗證過才宣告」——它知道 staging 到底刪掉了沒有
                        # （剛殺完 pip 程序樹之後,Windows 常常還鎖著那些檔案）。
                        # 這裡曾經無條件覆蓋成「已清乾淨」,把它辛苦查證的事實丟掉,
                        # 然後對著一個還躺著 600MB 的磁碟說「乾淨了」。
                        told = self._cancel_message(result)
                        self.sd_status_var.set(told.splitlines()[0])
                        self._append_desktop_log(told)
                        messagebox.showinfo("已取消",
                                            "建置已中止。沒有建立新版本，Store 樹維持原狀。\n\n" + told)
                    elif result.ok:
                        self._sd_last_store = result.root
                        self._sd_last_app = result.app_id
                        self._sd_last_version = result.version
                        self._sd_last_package = result.root
                        self.sd_status_var.set(result.summary())
                        self.sd_open_button.configure(state="normal")
                        self.sd_export_full_button.configure(state="normal")
                        self.sd_export_update_button.configure(state="normal")
                        self.sd_gc_button.configure(state="normal")
                        self._refresh_deliver_versions()     # 剛建好的這一版要能被選到
                        self._append_desktop_log("")
                        self._append_desktop_log(f"✓ Store 根：{result.root}")
                        self._append_desktop_log(
                            f"✓ 版本 {result.version}：{result.version_mb:.0f} MB"
                            f"（本次磁碟新增 {result.added_mb:.0f} MB，"
                            + ("runtime 重用" if result.runtime_reused else "新建 runtime") + "）")
                        for warning in result.warnings:
                            self._append_desktop_log("⚠ " + warning)

                        # 只講「真的發生了什麼」——以前這裡會說 User 端會自動套用，
                        # 但那個 pending 只寫在建置機自己這棵樹上。
                        lines = [f"Store 樹已更新：{result.root}",
                                 f"版本 {result.version}　·　本次新增 {result.added_mb:.0f} MB"
                                 f"　·　{result.duration_seconds:.0f} 秒", ""]
                        if result.pending_set:
                            lines += [
                                "這棵樹上：已設為「待套用」，下次啟動時會換到這一版；",
                                "　　      若它啟動失敗，會自動退回上一版。",
                                "",
                                "已經交付出去的機器不會自動拿到這一版。要發給它們：",
                                "　·　按「匯出更新包（給已部署的機器）…」，",
                                "　　  把包放進更新來源，或在目標機用 admin.bat 套用。",
                                "　·　設了更新來源的機器：下次啟動時會在背景把新版抓回來、",
                                "　　  驗證、設為待套用——要「再下一次」啟動才會真的換版。",
                            ]
                        else:
                            lines += ["這是這個 App 的第一個版本，已直接設為目前版本。", "",
                                      "交付：按「匯出完整交付（給新機器）…」，"
                                      "把產生的整個資料夾複製給 User。"]
                        lines += ["",
                                  f"User 端入口：{'、'.join(result.entry_bats) or '(無)'}",
                                  "（雙擊後視窗出現，還要在「工作流程」下拉按一次「Start」才會看到 App。）"]
                        if result.removed_start_bat:
                            messagebox.showwarning(
                                "建立完成（入口檔改變了）",
                                "\n".join(lines) + "\n\n"
                                "⚠ 這棵樹現在有多個 App，通用的 start.bat 已被移除。\n"
                                "　 已經教會 User「雙擊 start.bat」的機器要改用上面列出的檔案。")
                        else:
                            messagebox.showinfo("建立完成", "\n".join(lines))
                    else:
                        self.sd_status_var.set("建置失敗。")
                        for error in result.errors:
                            self._append_desktop_log("✗ " + error)
                        messagebox.showerror("建置失敗", "\n\n".join(result.errors))
                elif kind == "desktop_done":
                    self._set_desktop_busy(False)
                    result = payload
                    if getattr(result, "cancelled", False):
                        told = self._cancel_message(result)
                        self.sd_status_var.set(told.splitlines()[0])
                        self._append_desktop_log(told)
                        messagebox.showinfo("已取消",
                                            "建置已中止，沒有產生可交付資料夾。\n\n" + told)
                    elif result.ok:
                        self._sd_last_store = None      # fat 包沒有 store 可匯出/回收
                        self._sd_last_package = result.package_dir
                        self.sd_status_var.set(result.summary())
                        self.sd_open_button.configure(state="normal")
                        self._append_desktop_log("")
                        self._append_desktop_log(f"✓ 交付資料夾：{result.package_dir}")
                        self._append_desktop_log(f"  大小：{result.size_mb:.0f} MB")
                        for warning in result.warnings:
                            self._append_desktop_log("⚠ " + warning)
                        body = (f"交付資料夾已建立：\n{result.package_dir}\n\n"
                                f"大小 {result.size_mb:.0f} MB　·　{result.duration_seconds:.0f} 秒\n\n"
                                "把整個資料夾交給 User：\n" + USER_STEPS + "\n"
                                "（資料夾裡的「讀我-使用說明.txt」寫的是同樣這幾步。）")
                        if result.warnings:
                            messagebox.showwarning(
                                "建立完成（有需要注意的事）",
                                body + "\n\n⚠ " + "\n⚠ ".join(result.warnings))
                        else:
                            messagebox.showinfo("建立完成", body)
                    else:
                        self.sd_status_var.set("建置失敗。")
                        for error in result.errors:
                            self._append_desktop_log("✗ " + error)
                        detail = "\n\n".join(result.errors)
                        if result.log_path and Path(result.log_path).is_file():
                            self._append_desktop_log(f"  完整記錄：{result.log_path}")
                            if messagebox.askyesno(
                                    "建置失敗",
                                    detail + f"\n\n完整記錄：\n{result.log_path}\n\n要現在打開嗎？"):
                                os.startfile(result.log_path)  # type: ignore[attr-defined]
                        else:
                            messagebox.showerror("建置失敗", detail)
                elif kind == "validation_done":
                    code, validation_dir, cancelled = payload
                    self._set_busy(False)
                    if cancelled:
                        self.status_var.set("實機驗證已取消。")
                    elif code == 0:
                        self.status_var.set("實機驗證通過：補給包、暖機與 Tauri 畫面皆正常。")
                        messagebox.showinfo("驗證通過", f"Tauri 實機驗證通過。\n\n證據與截圖：\n{validation_dir}")
                    else:
                        self.status_var.set("實機驗證失敗，請查看紀錄與 validation-result.json。")
                        messagebox.showerror("驗證失敗", f"請查看紀錄與：\n{validation_dir / 'validation-result.json'}")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _show_success(self, dest: Path) -> None:
        summary = "打包完成。"
        manifest = dest / "provision.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                count = len(data.get("tools", []))
                total = sum(int(t.get("total_bytes", 0)) for t in data.get("tools", []))
                summary = f"打包完成：{count} 個工具，共 {human_size(total)}。"
            except (OSError, ValueError, TypeError):
                pass
        self.status_var.set(summary)
        self.open_button.configure(state="normal")
        tools = []
        try:
            tools = [str(t["tool_id"]) for t in data.get("tools", [])]
        except (NameError, TypeError, KeyError):
            pass
        source_root = dest / "source-packages"
        if source_root.is_dir():
            for manifest in sorted(source_root.glob("*/source-manifest.json")):
                try:
                    source_id = str(json.loads(manifest.read_text(encoding="utf-8"))["tool_id"])
                    if source_id not in tools:
                        tools.append(source_id)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
        self.validation_tool.configure(values=tools)
        if tools:
            self.validation_tool_var.set(tools[0])
            self.validate_button.configure(state="normal")
        messagebox.showinfo("打包完成", f"{summary}\n\n輸出位置：\n{dest}")

    def _append_log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _open_dest(self) -> None:
        dest = Path(self.dest_var.get()).resolve()
        if not dest.exists():
            messagebox.showwarning("找不到輸出", f"輸出資料夾不存在：\n{dest}")
            return
        if os.name == "nt":
            os.startfile(dest)  # type: ignore[attr-defined]
        else:  # pragma: no cover
            subprocess.Popen(["xdg-open", str(dest)])

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno("建置仍在執行", "關閉視窗會取消目前工作。確定要離開嗎？"):
                return
            # 兩條建置路徑各有各的取消機制。上一版只呼叫了 _active_process.cancel()，
            # 而 Streamlit 桌面建置從不指派 _active_process——於是視窗關了、Python
            # 走了，pip 卻變成孤兒繼續往 .staging-* 寫幾百 MB，沒有人會清掉它。
            self._sd_cancel.set()
            self._active_process.cancel()
            self._worker.join(timeout=20)
            if self._worker.is_alive():
                messagebox.showwarning(
                    "還在收尾",
                    "已送出取消，但工作還沒結束（pip 可能正在寫檔）。\n"
                    "再等幾秒；若要立刻離開，殘留的暫存資料夾（.staging-*）\n"
                    "會在下次建置時自動清掉。")
        self.destroy()


def main() -> int:
    if sys.version_info[:2] != (3, 11):
        messagebox.showerror("Python 版本不符", "請使用 Python 3.11 啟動打包 GUI。")
        return 2
    ProvisionApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Launch the CIM platform from the Native Agent store (B-2 phase 1).

The exact contract of nativeApp's portable ``start.bat``, re-pointed at the
agent's immutable version slots:

    active.json (agent 的切換權威)
      → versions/<ver>/engine/engine.py            CIM_ENGINE_EXE
      → shell.blobref.json → blobs → deps/shells/  要跑的 cim-light.exe
      → data/<project-key>/{logs,tool-venvs,…}      四個 CIM_* env + cwd

Updating = ``NativeAgent.update("cim-platform", <channel>)`` against a P0
release directory's ``offline-channel``; rolling back = the agent's own
rollback. This launcher only READS ``active.json`` — it never mutates state,
so a running platform keeps its resolved paths until restarted (cold-start
switch semantics, same as the Streamlit store).

``--dry-run`` prints the resolved plan as JSON without spawning anything —
that is the headless-testable surface; spawning the real shell additionally
needs a real WebView2 machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from provision_builder.blob_store import FileBlobStore

APP_ID = "cim-platform"
SHELL_BLOBREF = "shell.blobref.json"

DATA_SUBDIRS = ("logs", "tool-venvs", "deppack-cache", "wheel-store")


class LaunchError(Exception):
    """The platform cannot be launched; message says what to fix."""


def project_key(project_dir: Path | str) -> str:
    """`<safe-foldername>-<sha256(abspath)[:8]>` — byte-for-byte the start.bat rule,
    so a project keeps its data dir when a deployment migrates onto the store."""
    p = os.path.abspath(str(project_dir))
    name = os.path.basename(p.rstrip("\\/")) or "project"
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    return safe + "-" + hashlib.sha256(p.lower().encode()).hexdigest()[:8]


def _read_active(root: Path) -> dict:
    path = root / "applications" / APP_ID / "active.json"
    if not path.is_file():
        raise LaunchError(
            f"還沒有啟用中的平台版本:{path} 不存在。\n"
            f"  先安裝:NativeAgent.update(\"{APP_ID}\", <channel>)"
            "（update source 可直接指向 release 目錄的 offline-channel\\）")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LaunchError(f"active.json 損壞:{path}({exc})") from exc
    if not doc.get("version") or not doc.get("path"):
        raise LaunchError(f"active.json 缺欄位:{path}")
    return doc


def _materialize_shell(root: Path, version_dir: Path) -> Path:
    ref_path = version_dir / SHELL_BLOBREF
    if not ref_path.is_file():
        raise LaunchError(
            f"版本缺 {SHELL_BLOBREF}:{version_dir}\n"
            "  這不是 build_platform_napp 產的平台版本，或指定 --shell 覆蓋")
    try:
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
        digest, name = ref["sha256"], ref.get("name", "cim-light.exe")
    except (ValueError, KeyError) as exc:
        raise LaunchError(f"{SHELL_BLOBREF} 格式不對({exc})") from exc

    target = root / "deps" / "shells" / digest / name
    if target.is_file():
        return target
    blobs = FileBlobStore(root / "blobs")
    if not blobs.has(digest):
        raise LaunchError(
            f"殼的 blob 不在本機:{digest[:16]}…\n"
            "  代表安裝不完整;重跑 agent update（它會 _pull_blobs 補齊）")
    blobs.verify(digest)
    return blobs.link_into(digest, target)


def resolve_launch(root: Path | str, *, project_dir: Path | str | None = None,
                   shell_override: Path | str | None = None,
                   python_exe: Path | str | None = None) -> dict:
    """The full launch plan (shell, cwd, env) — pure resolution, zero mutation
    except creating the per-project data dirs (derived state, delete = rebuild)."""
    root = Path(root)
    active = _read_active(root)
    version_dir = Path(active["path"])
    engine_py = version_dir / "engine" / "engine.py"
    if not engine_py.is_file():
        raise LaunchError(
            f"啟用版本缺 engine:{engine_py}\n"
            "  版本目錄可能被手動改過;用 agent rollback 退回上一版或重新 update")

    shell = Path(shell_override or os.environ.get("CIM_TAURI_EXE") or
                 _materialize_shell(root, version_dir))
    if not shell.is_file():
        raise LaunchError(f"找不到 Tauri 殼:{shell}")

    python = Path(python_exe or os.environ.get("CIM_ENGINE_PYTHON") or sys.executable)

    if project_dir:
        project = Path(project_dir)
        key = project_key(project)  # 外部專案:路徑穩定 → key 穩定（start.bat 規則）
    else:
        project = version_dir / "engine"
        # 內建專案的路徑「每版都不同」（versions/<ver>/engine），拿它算 key 會讓
        # 每次更新重置 user data——違反「data 與版本分離」。固定 key。
        key = "engine-default"
    if not (project / "engine.py").is_file():
        raise LaunchError(f"--project 資料夾沒有 engine.py:{project}")
    data_dir = root / "applications" / APP_ID / "data" / key
    for sub in DATA_SUBDIRS:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    env = {
        "CIM_ENGINE_EXE": str(project / "engine.py"),
        "CIM_ENGINE_PYTHON": str(python),
        "CIM_TOOL_VENVS_DIR": str(data_dir / "tool-venvs"),
        "CIM_DEPPACK_CACHE": str(data_dir / "deppack-cache"),
        "CIM_WHEEL_STORE": str(data_dir / "wheel-store"),
        "CIM_LOG_DIR": str(data_dir / "logs"),
        "PYTHONUTF8": "1",
    }
    return {
        "version": active["version"],
        "shell": str(shell),
        "cwd": str(data_dir),  # 殼的 resolve_log_dir() = cwd\logs（start.bat 的 cwd 技巧）
        "env": env,
        "project": str(project),
        "project_key": key,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="platform_launcher",
        description="從 Native Agent store 啟動 CIM 平台（讀 active.json，不改任何狀態）")
    parser.add_argument("--root", required=True, type=Path, help="agent 資料根（含 applications\\）")
    parser.add_argument("--project", type=Path, default=None,
                        help="載入其它專案（預設 = 啟用版本內建的 engine）")
    parser.add_argument("--shell", type=Path, default=None, help="覆蓋 Tauri 殼路徑")
    parser.add_argument("--dry-run", action="store_true", help="只印解析結果（JSON），不啟動")
    args = parser.parse_args(argv)

    try:
        plan = resolve_launch(args.root, project_dir=args.project, shell_override=args.shell)
    except LaunchError as exc:
        print(f"[FAIL] {exc}")
        return 2

    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    print(f"[platform] version : {plan['version']}")
    print(f"[platform] project : {plan['project']}")
    print(f"[platform] data    : {plan['cwd']}")
    print(f"[platform] shell   : {plan['shell']}")
    process = subprocess.Popen([plan["shell"]], cwd=plan["cwd"],
                               env={**os.environ, **plan["env"]})
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())

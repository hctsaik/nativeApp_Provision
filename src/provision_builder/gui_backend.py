"""打包 GUI 的可測試後端。

GUI 本身只處理畫面；掃描沿用 scan.py，正式建置則啟動既有 provision.py CLI，
確保 GUI 與 CLI 的產物、錯誤處理及增量語意完全一致。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .gateway import PlatformGateway
from .scan import ScanResult, make_subprocess_loader, scan_project
from .source_pack import SourceModule, discover_source_modules, package_source_modules


@dataclass(frozen=True)
class BuildOptions:
    project_root: Path
    dest: Path
    tool_ids: tuple[str, ...]
    force: bool = False
    threshold_mb: int = 100
    python_cmd: str | None = None
    source_modules: tuple[SourceModule, ...] = ()
    launch_mode: str = "portable"


@dataclass(frozen=True)
class ValidationOptions:
    provision_dir: Path
    validation_dir: Path
    project_root: Path
    tool_id: str


def discover_tools(project_root: Path, *, python_cmd: list[str] | None = None) -> ScanResult:
    """掃描可打包工具；驗證專案與 YAML 的方式和正式 build 相同。"""
    gateway = PlatformGateway(Path(project_root), python_cmd=python_cmd)
    return scan_project(gateway.engine_root, make_subprocess_loader(gateway.python_cmd))


def build_command(options: BuildOptions, *, repo_root: Path | None = None) -> list[str]:
    """產生 GUI 要執行的 CLI 命令。固定鎖定平台的 win_amd64/cp311。"""
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable,
        "-u",
        str(root / "provision.py"),
        "build",
        str(Path(options.project_root).resolve()),
        "--dest",
        str(Path(options.dest).resolve()),
        "--platform",
        "win_amd64",
        "--python-version",
        "3.11",
        "--abi",
        "cp311",
        "--big-threshold-mb",
        str(options.threshold_mb),
    ]
    if options.tool_ids:
        cmd += ["--tools", ",".join(options.tool_ids)]
    if options.force:
        cmd.append("--force")
    if options.python_cmd:
        cmd += ["--python", options.python_cmd]
    cmd += ["--launch-mode", options.launch_mode]
    return cmd


def validation_command(options: ValidationOptions, *, repo_root: Path | None = None) -> list[str]:
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    return [
        "node", str(root / "e2e" / "validate_package.mjs"),
        "--provision", str(Path(options.provision_dir).resolve()),
        "--validation-dir", str(Path(options.validation_dir).resolve()),
        "--project", str(Path(options.project_root).resolve()),
        "--tool", options.tool_id,
        "--python", sys.executable,
        "--keep-open", "true",
    ]


class BuildProcess:
    """可取消的 CLI 子程序包裝；逐行回報輸出，供 Tk worker thread 使用。"""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self.cancelled = False

    def run(self, options: BuildOptions, on_line: Callable[[str], None]) -> int:
        self.cancelled = False
        if options.source_modules:
            package_source_modules(list(options.source_modules), options.dest, log=on_line)
        dependency_ids = tuple(m.tool_id for m in options.source_modules if m.requires)
        if options.source_modules and not dependency_ids:
            on_line("選取的 Module 沒有額外 Python 相依；原始碼包已完成，不需建立 dependency pack。")
            return 0
        if options.source_modules:
            options = BuildOptions(
                options.project_root, options.dest, dependency_ids, options.force,
                options.threshold_mb, options.python_cmd, options.source_modules,
                options.launch_mode,
            )
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            build_command(options),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=flags,
        )
        assert self._process.stdout is not None
        for line in self._process.stdout:
            on_line(line.rstrip("\r\n"))
        return self._process.wait()

    def cancel(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        self.cancelled = True
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        else:  # pragma: no cover - 產品目標是 Windows
            process.terminate()


class ValidationProcess(BuildProcess):
    def run(self, options: ValidationOptions, on_line: Callable[[str], None]) -> int:
        self.cancelled = False
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        self._process = subprocess.Popen(
            validation_command(options), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert self._process.stdout is not None
        for line in self._process.stdout:
            on_line(line.rstrip("\r\n"))
        return self._process.wait()

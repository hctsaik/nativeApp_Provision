"""共用小工具：雜湊、大小格式化、subprocess 執行、主控台編碼防護。

只用 stdlib（SPEC D2）。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> tuple[str, int]:
    """串流計算單檔 sha256 + 大小（torch 級 2GB 檔不可一次讀進記憶體）。

    與平台 `core.deppack._sha256_file` 同演算法（sha256 of raw bytes），
    因此本工具算出的雜湊可與 deppack.json 直接比對。
    """
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def human_size(num_bytes: int) -> str:
    """人類可讀大小（REPORT.md 用）。1536 → '1.5 KB'。"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"  # pragma: no cover - 迴圈已涵蓋


def run(cmd: list[str], *, timeout: int | None = None) -> tuple[bool, str]:
    """執行外部指令，回 (ok, 訊息)。失敗收斂成 (False, stderr 尾段)。

    與平台 tools/*.py 同樣的形狀（CREATE_NO_WINDOW 避免 Windows 彈黑窗）。
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, f"逾時（{timeout}s）：{' '.join(cmd[:3])} ..."
    except (OSError, ValueError) as exc:
        return False, f"無法執行 {cmd[0]!r}：{exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        tail = "\n".join(err.splitlines()[-15:]) if err else f"exit code {proc.returncode}"
        return False, tail
    return True, (proc.stdout or "").strip()


def guard_console_encoding() -> None:
    """CP950 繁中主控台印到非 CP950 字元會炸；一律 errors='replace'。

    平台 tools/build_deppack.py 有同樣的防護（實際踩過）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

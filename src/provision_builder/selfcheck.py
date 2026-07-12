"""離線可裝自檢（SPEC §8.2）—— 在**開發機**就證明「這包離線裝得起來」。

做法沿用平台 `tools/plugin_pack.py::_offline_dryrun`：用
`pip download --no-index --find-links=<本地wheels>` 對目標平台標籤重解一次依賴圖。
比「建 venv 真裝」快得多，而且可以在任何平台的開發機驗證 win/cp311 的可解性。

抓得到的典型問題：某個間接相依只有 sdist 沒有目標平台 wheel、requires 寫錯版號、
或大 wheel 隔離後 find-links 沒把 big-deps 一起帶上。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ._util import run
from .gateway import Target


def build_offline_resolve_command(
    python_cmd: list[str],
    requires: list[str],
    find_links: list[Path],
    target: Target,
    dest: Path,
) -> list[str]:
    """組 `pip download --no-index` 指令（純函式，方便測試斷言旗標齊全）。

    `--find-links` 可給多個：工具自己的 wheels/ + 頂層 big-deps/
    （大 wheel 被隔離走了，少了它 pip 一定解不開）。
    """
    cmd = [*python_cmd, "-m", "pip", "download", *requires, "--no-index"]
    for link in find_links:
        cmd.append(f"--find-links={link}")
    cmd += [
        "--only-binary=:all:",
        "--platform", target.platform_tag,
        "--python-version", target.python_version,
        "--abi", target.abi,
        "--implementation", "cp",
        "--dest", str(dest),
    ]
    return cmd


def offline_resolve(
    python_cmd: list[str],
    requires: list[str],
    find_links: list[Path],
    target: Target,
    *,
    timeout: int = 600,
) -> tuple[bool, str]:
    """回 (ok, 訊息)。ok=False 時訊息含 pip 的錯誤尾段（會點名缺哪個套件）。"""
    with tempfile.TemporaryDirectory(prefix="provision-selfcheck-") as td:
        cmd = build_offline_resolve_command(python_cmd, requires, find_links, target, Path(td))
        return run(cmd, timeout=timeout)

"""provision.json 讀寫（SPEC §5.1）—— 補給包的總 manifest。

它回答「工廠端手上這包是誰、什麼時候、對哪個 commit、包了哪些工具、大東西有哪些」。
逐 wheel 的雜湊仍然住在各 pack 的 deppack.json（平台的權威格式）；本檔只做索引與導覽。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import PROVISION_FORMAT_VERSION, PROVISION_MANIFEST, __version__
from ._util import run


def _git(project_root: Path, *args: str) -> str | None:
    ok, out = run(["git", "-C", str(project_root), *args], timeout=30)
    return out.strip() if ok and out.strip() else None


def git_info(project_root: Path) -> dict:
    """平台 commit + 各 submodule 指標。非 git 專案 → 全 None（不報錯）。"""
    head = _git(project_root, "rev-parse", "HEAD")
    submodules: dict[str, str] = {}
    status = _git(project_root, "submodule", "status")
    if status:
        for line in status.splitlines():
            # 格式：" <sha> <path> (<describe>)"，前綴可能是 '-'/'+'/'U'
            parts = line.strip().lstrip("-+U").split()
            if len(parts) >= 2:
                submodules[parts[1]] = parts[0]
    return {"platform_commit": head, "submodules": submodules}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_provision_manifest(
    *,
    project_root: Path,
    target: dict,
    scanned_roots: list[str],
    big_threshold_mb: int,
    tools: list[dict],
    big_deps: list[dict],
    skipped_tools: list[dict],
    failed_tools: list[dict],
    created_at: str | None = None,
) -> dict:
    """組出 provision.json 的 dict（鍵順序固定，可 diff）。"""
    return {
        "format_version": PROVISION_FORMAT_VERSION,
        "builder_version": __version__,
        "created_at": created_at or utc_now(),
        "source_project": str(Path(project_root).resolve()),
        "git": git_info(Path(project_root)),
        "target": dict(target),
        "scanned_roots": list(scanned_roots),
        "big_threshold_mb": int(big_threshold_mb),
        "tools": list(tools),
        "big_deps": list(big_deps),
        "skipped_tools": list(skipped_tools),
        "failed_tools": list(failed_tools),
    }


def write_provision_manifest(manifest: dict, root: Path) -> Path:
    path = Path(root) / PROVISION_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def read_provision_manifest(root: Path) -> dict | None:
    """讀既有的 provision.json（供增量重建判斷）。不存在或壞掉 → None（當作全新產包）。"""
    path = Path(root) / PROVISION_MANIFEST
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collect_big_deps(tools: list[dict], big_deps_dir: Path) -> list[dict]:
    """從各工具的 big_wheels 反推 big-deps 清單（含 used_by 引用計數）。

    used_by 是「移除某工具時能不能刪這個大 wheel」的唯一依據（bigdeps.exclusive_wheels）。
    """
    from ._util import sha256_file

    used_by: dict[str, list[str]] = {}
    for tool in tools:
        for name in tool.get("big_wheels", []):
            used_by.setdefault(str(name), []).append(str(tool["tool_id"]))

    entries: list[dict] = []
    for name in sorted(used_by):
        path = Path(big_deps_dir) / name
        if path.is_file():
            digest, size = sha256_file(path)
        else:  # 檔案不在（被使用者搬走）→ 仍列出，size/sha 留空讓 verify 報「未就位」
            digest, size = "", 0
        entries.append({
            "name": name,
            "sha256": digest,
            "size": size,
            "used_by": sorted(used_by[name]),
        })
    return entries

"""Module 原始碼包：與 dependency pack 分開、可獨立更新與驗證。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .scan import ScanError, make_subprocess_loader

SOURCE_PACKAGES_DIRNAME = "source-packages"
SOURCE_MANIFEST = "source-manifest.json"
_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv", ".git"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


@dataclass(frozen=True)
class SourceModule:
    tool_id: str
    name: str
    version: str
    folder: Path
    requires: tuple[str, ...]
    enabled: bool
    category: str = "module"
    runner: str = ""


def discover_source_modules(module_root: Path, python_cmd: list[str]) -> list[SourceModule]:
    """接受單一 Module 目錄，或包含多個 Module 子目錄的根。"""
    root = Path(module_root).resolve()
    if not root.is_dir():
        raise ScanError(f"Module 資料夾不存在：{root}")
    direct = root / "plugin.yaml"
    paths = [direct] if direct.is_file() else sorted(root.glob("*/plugin.yaml"))
    if not paths:
        raise ScanError(f"{root} 內找不到 plugin.yaml 或 */plugin.yaml")
    loaded = make_subprocess_loader(python_cmd)(paths)
    modules: list[SourceModule] = []
    seen: set[str] = set()
    for path in paths:
        entry = loaded.get(str(path)) or {}
        if not entry.get("ok"):
            raise ScanError(f"plugin.yaml 解析失敗：{path}\n  {entry.get('error', '未知錯誤')}")
        data = entry.get("data") or {}
        tool_id = str(data.get("id") or "").strip()
        if not tool_id:
            raise ScanError(f"plugin.yaml 缺少 id：{path}")
        if tool_id in seen:
            raise ScanError(f"Module 資料夾內有重複 id：{tool_id}")
        seen.add(tool_id)
        requires = tuple(str(v).strip() for v in (data.get("requires") or []) if str(v).strip())
        modules.append(SourceModule(
            tool_id=tool_id, name=str(data.get("name") or tool_id),
            version=str(data.get("version") or "0.0.0"), folder=path.parent,
            requires=requires, enabled=bool(data.get("enabled", True)),
            category=str(data.get("category") or "module"), runner=str(data.get("runner") or ""),
        ))
    return modules


def _iter_source_files(folder: Path):
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.is_file() and path.suffix.lower() not in _EXCLUDED_SUFFIXES:
            yield path, rel


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def package_source_module(module: SourceModule, dest: Path) -> dict:
    """原子性建立 source-packages/<id>；manifest 對每個來源檔案簽 hash。"""
    root = Path(dest).resolve() / SOURCE_PACKAGES_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    final = root / module.tool_id
    staging = Path(tempfile.mkdtemp(prefix=f".{module.tool_id}-", dir=root))
    try:
        source_dir = staging / "source"
        entries = []
        for path, rel in _iter_source_files(module.folder):
            target = source_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            entries.append({"path": rel.as_posix(), "sha256": _sha256(target), "size": target.stat().st_size})
        manifest = {
            "format_version": 1, "tool_id": module.tool_id, "name": module.name,
            "version": module.version, "created_at": datetime.now(timezone.utc).isoformat(),
            "requires": list(module.requires), "category": module.category, "runner": module.runner,
            "files": entries,
        }
        (staging / SOURCE_MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        # OneDrive/防毒軟體可能短暫鎖住舊目錄。每次用唯一備份名避免撞到先前殘留；
        # 新版已原子換位成功後，舊版清不掉只代表稍後需回收，不能誤判本次打包失敗。
        backup = root / f".{module.tool_id}.old-{uuid.uuid4().hex[:8]}"
        moved_old = False
        if final.exists():
            os.replace(final, backup)
            moved_old = True
        try:
            os.replace(staging, final)
        except Exception:
            if moved_old and backup.exists() and not final.exists():
                os.replace(backup, final)
            raise
        if moved_old and backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError:
                pass  # OneDrive lock：保留隱藏舊版，下一次維護再清，不影響新包
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def package_source_modules(modules: list[SourceModule], dest: Path, log=print) -> list[dict]:
    results = []
    for module in modules:
        log(f"[原始碼] {module.tool_id} {module.version} → {SOURCE_PACKAGES_DIRNAME}\\{module.tool_id}")
        results.append(package_source_module(module, dest))
    return results

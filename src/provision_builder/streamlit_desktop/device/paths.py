"""The deployed tree's shape, in one place.

Every path is <trusted root> / <validated identifier>; nothing here accepts a
path from a manifest. bootstrap, updater and gc all resolve through this module
so the layout exists exactly once.
"""

from __future__ import annotations

import json
from pathlib import Path

if __package__:
    from . import integrity
    from .identifiers import validate_identifier
else:
    import integrity
    from identifiers import validate_identifier

MANIFEST_NAME = "app-package.json"
MANIFEST_SCHEMA = 2


class LayoutError(Exception):
    pass


class AppPaths:
    def __init__(self, root: Path, app_id: str):
        self.root = Path(root)
        self.app_id = validate_identifier(app_id, "app_id")
        self.app_dir = self.root / "apps" / self.app_id
        self.state_dir = self.app_dir / "state"
        self.versions_dir = self.app_dir / "versions"
        self.staging_dir = self.app_dir / "staging"
        self.data_dir = self.app_dir / "data"
        self.deps_dir = self.root / "deps"

    def version_dir(self, version: str) -> Path:
        return self.versions_dir / validate_identifier(version, "version")

    def ensure_data_dirs(self) -> None:
        for name in ("logs", "cache", "home", "tmp", "leases"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)

    def config(self) -> dict:
        """apps/<app>/config.json — admin-editable; absent means defaults."""
        path = self.app_dir / "config.json"
        try:
            data = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise LayoutError(f"config.json 不是合法 JSON:{path}({exc})") from exc
        return data if isinstance(data, dict) else {}


def list_app_ids(root: Path) -> list[str]:
    apps = Path(root) / "apps"
    if not apps.is_dir():
        return []
    found = []
    for child in sorted(apps.iterdir()):
        if child.is_dir() and (child / "state").is_dir():
            found.append(child.name)
    return found


def load_manifest(version_dir: Path) -> dict:
    path = Path(version_dir) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise LayoutError(f"缺 {MANIFEST_NAME}:{path}") from exc
    except ValueError as exc:
        raise LayoutError(f"{MANIFEST_NAME} 不是合法 JSON:{exc}") from exc
    if not isinstance(data, dict):
        raise LayoutError(f"{MANIFEST_NAME} 不是 JSON object:{path}")
    return data


def verify_version(paths: AppPaths, version: str, *, deep: bool) -> list[str]:
    """Spec §6: identity + sentinel (+ full byte check when deep)."""
    problems: list[str] = []
    vdir = paths.version_dir(version)
    if not vdir.is_dir():
        return [f"版本目錄不存在:{vdir}"]
    if not integrity.is_complete(vdir):
        problems.append(f"版本 {version} 缺 .complete(安裝未完成)")
    try:
        manifest = load_manifest(vdir)
    except LayoutError as exc:
        return problems + [str(exc)]
    if manifest.get("app_id") != paths.app_id:
        problems.append(f"manifest app_id 不符:{manifest.get('app_id')!r} != {paths.app_id!r}")
    if manifest.get("version") != version:
        problems.append(f"manifest version 不符:{manifest.get('version')!r} != {version!r}")
    if not manifest.get("runtime_fingerprint"):
        problems.append("manifest 缺 runtime_fingerprint")
    # The shell may live in the version (legacy) or in the shared store (current).
    if not manifest.get("shell_fingerprint") and not manifest.get("shell_executable"):
        problems.append("manifest 缺 shell_fingerprint(或舊版的 shell_executable)")
    if deep and not problems:
        problems.extend(integrity.verify_tree(vdir))
    return problems

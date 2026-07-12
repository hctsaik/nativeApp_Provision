"""Update providers.

The first (and default) provider reads a plain directory — a USB stick or a
network share — because this product line's user machines are offline. HTTP or
Fleet push implement the same three methods later. Whatever the transport, the
updater trusts nothing until manifests and hashes verify.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

if __package__:
    from .identifiers import validate_identifier
else:
    from identifiers import validate_identifier


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseMetadata:
    app_id: str
    version: str
    revision: str          # changes when the same version is re-cut (retry gate)
    runtime_fingerprint: str
    # The Tauri shell is shared like the runtime and travels in the payload
    # (export_update writes shells/<fp>/). A release that needs a shell this
    # machine has never seen must be able to fetch it, or the update installs
    # and then opens a window onto nothing. Optional: pre-shell-store releases
    # (and every release whose shell still lives inside the version dir) omit it.
    shell_fingerprint: str | None = None


class UpdateProvider(Protocol):
    def get_latest_release(self, app_id: str, current_version: str) -> ReleaseMetadata | None: ...
    def download_app(self, release: ReleaseMetadata, destination: Path) -> None: ...
    def download_runtime(self, release: ReleaseMetadata, destination: Path) -> None: ...
    def download_shell(self, release: ReleaseMetadata, destination: Path) -> None: ...
    def has_runtime(self, release: ReleaseMetadata) -> bool: ...
    def has_shell(self, release: ReleaseMetadata) -> bool: ...


class FolderUpdateProvider:
    """<source>/<app-id>/release.json + versions/<ver>/ + runtimes/<fp>/ + shells/<fp>/.

    This is exactly what store_builder.export_update() writes, so an update share
    is "the folder you exported into" and nothing else.
    """

    def __init__(self, source_dir: Path):
        self.source = Path(source_dir)

    def _app_root(self, app_id: str) -> Path:
        return self.source / validate_identifier(app_id, "app_id")

    def get_latest_release(self, app_id: str, current_version: str) -> ReleaseMetadata | None:
        path = self._app_root(app_id) / "release.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderError(f"release.json 不可讀:{path}({exc})") from exc
        try:
            shell_fp = data.get("shell_fingerprint")
            release = ReleaseMetadata(
                app_id=validate_identifier(data["app_id"], "release app_id"),
                version=validate_identifier(data["version"], "release version"),
                revision=str(data.get("revision") or ""),
                runtime_fingerprint=validate_identifier(
                    data["runtime_fingerprint"], "release runtime_fingerprint"),
                shell_fingerprint=(validate_identifier(shell_fp, "release shell_fingerprint")
                                   if shell_fp else None),
            )
        except (KeyError, ValueError) as exc:
            raise ProviderError(f"release.json 內容不合法:{path}({exc})") from exc
        if release.app_id != app_id:
            raise ProviderError(
                f"release.json 是別的 app 的:{release.app_id!r} != {app_id!r}")
        return release

    def _copy(self, src: Path, destination: Path, what: str) -> None:
        if not src.is_dir():
            raise ProviderError(f"更新來源缺 {what}:{src}")
        # ONLY the sentinel is skipped — the target must earn it by verifying.
        # Filtering anything else (we used to drop __pycache__/*.pyc here) deletes
        # files that the tree's own files.json still declares, and the copy then
        # fails integrity on this machine with "重新複製一次" advice that can never
        # work. export_update() strips exactly this one name and nothing else.
        shutil.copytree(src, destination, ignore=shutil.ignore_patterns(".complete"))

    def download_app(self, release: ReleaseMetadata, destination: Path) -> None:
        self._copy(self._app_root(release.app_id) / "versions" / release.version,
                   destination, f"versions/{release.version}")

    # An incremental update carries no runtime and no shell by design — the target
    # already has them. "Do you have one?" therefore has to be answerable WITHOUT
    # attempting the copy: the alternative is discovering the answer as a failure,
    # which is what turned every incremental install into a dead end.
    def has_runtime(self, release: ReleaseMetadata) -> bool:
        return (self._app_root(release.app_id) / "runtimes"
                / release.runtime_fingerprint).is_dir()

    def has_shell(self, release: ReleaseMetadata) -> bool:
        if not release.shell_fingerprint:
            return False
        return (self._app_root(release.app_id) / "shells"
                / release.shell_fingerprint).is_dir()

    def download_runtime(self, release: ReleaseMetadata, destination: Path) -> None:
        self._copy(self._app_root(release.app_id) / "runtimes" / release.runtime_fingerprint,
                   destination, f"runtimes/{release.runtime_fingerprint}")

    def download_shell(self, release: ReleaseMetadata, destination: Path) -> None:
        if not release.shell_fingerprint:
            raise ProviderError("release.json 沒有 shell_fingerprint,無法取得 Tauri 殼")
        self._copy(self._app_root(release.app_id) / "shells" / release.shell_fingerprint,
                   destination, f"shells/{release.shell_fingerprint}")

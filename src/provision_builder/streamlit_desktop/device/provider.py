"""Update providers.

The first (and default) provider reads a plain directory — a USB stick or a
network share — because this product line's user machines are offline. HTTP or
Fleet push implement the same three methods later. Whatever the transport, the
updater trusts nothing until manifests and hashes verify.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

if __package__:
    from . import integrity
    from .identifiers import validate_identifier
    from .locks import hardlinks_unsupported
else:                                  # loose files in <ROOT>/bootstrap/
    import integrity
    from identifiers import validate_identifier
    from locks import hardlinks_unsupported


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


def _release_app_id(path: Path) -> str | None:
    """The app_id a release.json declares, or None if there is no readable one here.

    Never raises. It is asked of directories that may be anything at all (a USB stick's
    root, a folder the operator renamed to 「v1.1.0更新包」), and 「this is not a payload」
    is an ANSWER, not a failure. The id is validated: an app_id from a stick is
    untrusted input, and it is about to be used to build a path.
    """
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        return validate_identifier(data["app_id"], "release app_id")
    except (OSError, ValueError, KeyError, TypeError):
        return None


class UpdateProvider(Protocol):
    def get_latest_release(self, app_id: str, current_version: str) -> ReleaseMetadata | None: ...
    def download_app(self, release: ReleaseMetadata, destination: Path, *,
                     link_from: Path | None = None) -> None: ...
    def download_runtime(self, release: ReleaseMetadata, destination: Path) -> None: ...
    def download_shell(self, release: ReleaseMetadata, destination: Path) -> None: ...
    def has_runtime(self, release: ReleaseMetadata) -> bool: ...
    def has_shell(self, release: ReleaseMetadata) -> bool: ...


# ── dedup between version slots ──────────────────────────────────────────────
#
# A version directory is a WHOLE copy of the app. CV_Viewer's 84 MB DINOv2 weight has
# not changed in a year and it was written again into every slot — so five releases cost
# 481 MB on the factory PC to hold five copies of one identical file, and the store
# layout's central promise (「一次改版只搬十幾 MB」) was simply false.
#
# A hardlink makes the duplicate free: two names, one inode, one lot of bytes. It is safe
# here for one specific reason, and it is worth stating so that nobody has to re-derive it:
# A PUBLISHED VERSION DIRECTORY IS NEVER WRITTEN TO. It is staged elsewhere, verified,
# renamed into place, and from then on it is read-only by contract — bootstrap.py even
# sets PYTHONDONTWRITEBYTECODE in the launcher's environment so the running app cannot
# drop a .pyc beside its own code. Nothing opens a file inside a completed slot for
# writing, so no write can ever reach a shared inode. If that ever changes, this whole
# scheme breaks, and this is the comment that should stop it.
#
# Deleting is safe with no reasoning at all: a name goes, and the bytes go only when the
# LAST name goes. gc.py rmtree-ing one slot cannot take a byte another slot still uses.

def _files_index(slot: Path) -> dict[str, tuple[int, str]]:
    """relpath -> (size, sha256), read from a completed slot's own files.json.

    Free: every completed version already carries a sha256 per file — that is how this
    machine verified it in the first place. Nothing is re-hashed to build this.
    """
    try:
        data = json.loads((slot / integrity.FILES_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(e["path"]): (int(e.get("size", 0)), str(e.get("sha256", "")))
            for e in data.get("files", []) if e.get("path")}


def _prior_slot_index(versions_dir: Path | None) -> dict[str, tuple[Path, int, str]]:
    """relpath -> (the file in an existing COMPLETE version slot, size, sha256)."""
    index: dict[str, tuple[Path, int, str]] = {}
    if versions_dir is None or not Path(versions_dir).is_dir():
        return index
    try:
        slots = [p for p in Path(versions_dir).iterdir()
                 if p.is_dir() and not p.name.startswith(".") and integrity.is_complete(p)]
    except OSError:
        return index
    for slot in sorted(slots, reverse=True):       # newest name first: likeliest match
        for rel, (size, digest) in _files_index(slot).items():
            if digest and rel not in index:
                index[rel] = (slot / rel, size, digest)
    return index


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class _SlotLinker:
    """copytree's copy_function: hardlink what an existing version slot already holds.

    Degrades to a plain copy, silently and per-build, when the filesystem cannot do hard
    links at all — FAT/exFAT, i.e. most USB sticks, which spec §9.3 promises to support.
    That lesson was learned in locks.py; its errno/winerror table is reused rather than
    guessed at a second time.
    """

    def __init__(self, index: dict[str, tuple[Path, int, str]], destination: Path):
        self.index = index
        self.destination = Path(destination)
        self.linked = 0
        self.linked_bytes = 0
        self.supported = bool(index)

    def __call__(self, src, dst):
        prior = None
        if self.supported:
            try:
                rel = Path(dst).relative_to(self.destination).as_posix()
            except ValueError:
                rel = ""
            prior = self.index.get(rel) if rel else None
        if prior is not None:
            slot_file, size, digest = prior
            # Size is a stat and settles almost everything; only a same-path, same-size
            # candidate is worth hashing. That read then replaces the copy's read+WRITE.
            if os.path.getsize(src) == size and _sha256_file(src) == digest:
                try:
                    os.link(slot_file, dst)
                except OSError as exc:
                    if hardlinks_unsupported(exc):
                        self.supported = False     # FAT/exFAT: stop asking
                else:
                    self.linked += 1
                    self.linked_bytes += size
                    return dst
        return shutil.copy2(src, dst)


class FolderUpdateProvider:
    """<source>/<app-id>/release.json + versions/<ver>/ + runtimes/<fp>/ + shells/<fp>/.

    This is exactly what store_builder.export_update() writes, so an update share
    is "the folder you exported into" and nothing else.
    """

    def __init__(self, source_dir: Path, *, app_root: Path | None = None):
        self.source = Path(source_dir)
        # An update SOURCE holds <source>/<app_id>/…; a payload FOLDER *is* that
        # inner directory, and the operator is free to rename it ("v1.1.0更新包")
        # or copy it somewhere else. When the caller already knows exactly which
        # directory holds release.json, it says so and we never re-derive the
        # path from the app id — deriving it is how --install came to look for
        # <renamed>/../<app_id>/release.json and fail on a path nobody created.
        self._app_root_override = Path(app_root) if app_root is not None else None

    @classmethod
    def from_payload_dir(cls, payload_dir: Path) -> "FolderUpdateProvider":
        """Read release.json / versions/ / runtimes/ / shells/ from THIS folder,
        whatever it happens to be called."""
        payload_dir = Path(payload_dir)
        return cls(payload_dir.parent, app_root=payload_dir)

    def _app_root(self, app_id: str) -> Path:
        if self._app_root_override is not None:
            validate_identifier(app_id, "app_id")
            return self._app_root_override
        return self.source / validate_identifier(app_id, "app_id")

    # ── whose update IS this? ────────────────────────────────────────────────
    #
    # Every method below `get_latest_release` starts from an app_id the CALLER already
    # has, and the caller (bootstrap) gets that id from the apps ALREADY INSTALLED on
    # the machine. So the SECOND app on a factory PC could never arrive by the update
    # path at all: `--install` resolved the app against apps\, found only App A, asked
    # this provider for App A's release, got App B's release.json, and refused it as
    # 「別的 app 的」 — with the machine, the operator and the payload all perfectly
    # correct. A store that cannot receive a new app is not a store; it is one app with
    # extra steps.
    #
    # The provider cannot fix bootstrap's half (see its _resolve_app), but it CAN stop
    # being the reason the question is unanswerable: a payload knows which app it is
    # for, and an update source knows which apps it offers. Both are readable without
    # knowing the answer in advance, which is the whole point.

    def payload_app_id(self) -> str | None:
        """Which app is THIS payload folder for? Read out of its release.json.

        For `FolderUpdateProvider.from_payload_dir(...)` — the `--install <folder>`
        shape. The answer may be an app this machine has never heard of; that is
        exactly the case this exists for. None = no readable release.json here.
        """
        root = (self._app_root_override if self._app_root_override is not None
                else self.source)
        return _release_app_id(root / "release.json")

    def app_ids(self) -> list[str]:
        """Every app this update SOURCE offers — <source>/<app-id>/release.json.

        A machine polling a share can discover an app it does not have yet. Unreadable
        or foreign-looking directories are skipped rather than raised on: an update
        source is a folder on a USB stick, and it will have junk in it.
        """
        if self._app_root_override is not None:
            found = self.payload_app_id()
            return [found] if found else []
        if not self.source.is_dir():
            return []
        offered: list[str] = []
        try:
            children = sorted(self.source.iterdir())
        except OSError:
            return []
        for child in children:
            if not child.is_dir():
                continue
            app_id = _release_app_id(child / "release.json")
            if app_id and app_id not in offered:
                offered.append(app_id)
        return offered

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
            # THE CHECK STAYS — a provider that hands back another app's release is how
            # App B's bytes get installed into App A's version slot, and no amount of
            # convenience is worth that. What changes is that it stops being a dead end.
            # The operator hit this by doing everything right: they copied App B's
            # update folder onto a machine that runs App A, and `--install` (which
            # resolves the app from apps\, so it could only ever name App A) asked us
            # for the wrong one. Name the app the payload IS for, and the way in.
            raise ProviderError(
                f"這份更新包是 {release.app_id!r} 的,不是 {app_id!r} 的。\n"
                f"  更新包:{path}\n"
                f"  · 如果這台機器要更新的是 {release.app_id!r},請指定它:"
                f"--app {release.app_id} --install <這個資料夾>\n"
                f"  · 如果 {release.app_id!r} 還沒裝在這台機器上,更新包裝不上去:"
                "更新包只有版本,沒有 bootstrap\\、沒有啟動檔。"
                "第一次安裝一個新 App,要用建置機匯出的「完整交付」資料夾。")
        return release

    def _copy(self, src: Path, destination: Path, what: str, *,
              link_from: Path | None = None) -> None:
        if not src.is_dir():
            raise ProviderError(f"更新來源缺 {what}:{src}")
        # ONLY the sentinel is skipped — the target must earn it by verifying.
        # Filtering anything else (we used to drop __pycache__/*.pyc here) deletes
        # files that the tree's own files.json still declares, and the copy then
        # fails integrity on this machine with "重新複製一次" advice that can never
        # work. export_update() strips exactly this one name and nothing else.
        linker = _SlotLinker(_prior_slot_index(link_from), destination)
        shutil.copytree(src, destination, ignore=shutil.ignore_patterns(".complete"),
                        copy_function=linker)

    def download_app(self, release: ReleaseMetadata, destination: Path, *,
                     link_from: Path | None = None) -> None:
        """`link_from` = this app's versions\\ directory on THIS machine.

        Without it, staging an update copies the whole version out of the payload —
        including the 84 MB model file the machine is already running, byte for byte, in
        the version slot right next to the one being staged. The factory PC pays 84 MB
        of its disk for a second copy of a file it already has, on every release, and the
        「一次改版只搬十幾 MB」 the store layout is sold on is not true on the one machine
        that matters. With it, an unchanged file costs a directory entry.

        Safe by construction on this path, which is the reason it is offered here at all:
        the staging dir is a sibling of the version slots (same volume, so os.link can
        work), and updater.stage_release() runs integrity.verify_tree() over the STAGED
        tree — hashing whatever the link points at — before anything is renamed into
        place. A wrong link cannot be promoted; it is caught as a verification failure
        and the staging dir is destroyed. Optional and defaulted OFF, so a caller that
        does not pass it gets exactly today's behaviour.
        """
        self._copy(self._app_root(release.app_id) / "versions" / release.version,
                   destination, f"versions/{release.version}", link_from=link_from)

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

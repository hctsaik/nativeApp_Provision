"""Device update state machine (see 02_ARCHITECTURE.md §6-8)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from provision_builder import winfs
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import install_source, verify_napp
from provision_builder.napp.signing import Verifier
from provision_builder.package_errors import ArtifactMissing, HashMismatch, PackageDomainError
from provision_builder.package_services import YANKED, PackageService, Release
from native_agent.operations import OperationCancelled
from native_agent.state import AgentState

_CHUNK = 1024 * 1024

# Terminal outcomes of an update attempt.
START_ACTIVE = "START_ACTIVE"       # already on desired version
START_CACHED = "START_CACHED"       # remote unavailable / nothing to do; keep active
UPDATED = "UPDATED"                 # switched to a new healthy version
FAILED = "FAILED"                   # failed before activation; active untouched
ROLLED_BACK = "ROLLED_BACK"         # failed after activation; reverted
CANCELLED = "CANCELLED"             # cancelled at a safe stage; active untouched
SKIPPED_FAILED = "SKIPPED_FAILED"   # desired version is a known-bad one; not retried
SKIPPED_YANKED = "SKIPPED_YANKED"   # channel points at a yanked release


class HealthcheckFailed(Exception):
    pass


class IncompatiblePackage(PackageDomainError):
    code = "incompatible_package"


_META_SUFFIX = ".meta.json"
_VENV_COMPLETE = ".complete"


@dataclass
class UpdateOutcome:
    state: str
    active: str | None
    target: str | None = None
    error: str | None = None
    blobs_pulled: int = 0
    blobs_reused: int = 0
    venv_reused: bool = False
    details: dict = field(default_factory=dict)


Hook = Callable[[Path], None]
HealthHook = Callable[[Path], bool]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digester = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK):
            digester.update(chunk)
    return digester.hexdigest()


class NativeAgent:
    def __init__(
        self,
        data_root: Path | str,
        remote: PackageService,
        remote_blobs: FileBlobStore,
        *,
        verifier: Verifier | None = None,
        ensure_venv: Callable[[str, Path], None] | None = None,
        warmup: Hook | None = None,
        healthcheck: HealthHook | None = None,
        migrate: Hook | None = None,
        observe: HealthHook | None = None,
        expected_platform: dict | None = None,
    ):
        self.root = Path(data_root)
        self.remote = remote
        self.remote_blobs = remote_blobs
        self.verifier = verifier
        self.state = AgentState(self.root / "agent" / "state.db")
        self.blobs = FileBlobStore(self.root / "blobs")
        self._ensure_venv = ensure_venv
        self._warmup = warmup
        self._healthcheck = healthcheck or (lambda _p: True)
        self._migrate = migrate
        self._observe = observe or (lambda _p: True)
        # Device platform constraints (os/arch/python/abi). Empty → accept any
        # (lab default). A declared package field that disagrees is rejected.
        self.expected_platform = expected_platform or {}

    # ── layout helpers ──────────────────────────────────────────────────────

    def _app_dir(self, app_id: str) -> Path:
        return self.root / "applications" / app_id

    def _versions_dir(self, app_id: str) -> Path:
        return self._app_dir(app_id) / "versions"

    def _active_json(self, app_id: str) -> Path:
        return self._app_dir(app_id) / "active.json"

    def _runtimes_dir(self) -> Path:
        """Global runtime store: one venv per dependency fingerprint, shared by
        every app on the device (P1 convergence — the Store's deps/runtimes
        model). Legacy per-app ``venvs/`` are still honoured read-only."""
        return self.root / "deps" / "runtimes"

    def runtime_dir(self, fingerprint: str) -> Path:
        """Where the runtime for ``fingerprint`` lives (global store, with a
        fallback to a pre-convergence per-app venv if one is still complete)."""
        return self._runtimes_dir() / fingerprint

    def _legacy_venv_dir(self, app_id: str, fingerprint: str) -> Path:
        return self._app_dir(app_id) / "venvs" / fingerprint

    def _ensure_layout(self, app_id: str) -> None:
        for sub in ("versions", "staging", "data"):
            (self._app_dir(app_id) / sub).mkdir(parents=True, exist_ok=True)
        self._runtimes_dir().mkdir(parents=True, exist_ok=True)

    def read_active(self, app_id: str) -> dict | None:
        path = self._active_json(app_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    # ── public API ──────────────────────────────────────────────────────────

    def check(self, app_id: str, channel: str) -> Release | None:
        return self.remote.resolve(app_id, channel)

    def update(self, app_id: str, channel: str, *, force: bool = False) -> UpdateOutcome:
        """Synchronous update (unchanged behaviour): plan, then execute."""
        early, desired, active = self.plan_update(app_id, channel, force=force)
        if early is not None:
            return early
        op = self.state.begin_operation(
            app_id, from_version=active, to_version=desired.version,
            previous_active=active, desired_identity=desired.version, kind="update",
        )
        return self.execute_update(op, app_id, desired, active)

    def plan_update(self, app_id: str, channel: str, *, force: bool = False):
        """Return ``(early_outcome | None, desired, active)``.

        A non-None early_outcome means no operation is needed (already active,
        remote unavailable, yanked, or a known-bad version to skip).
        """
        self._ensure_layout(app_id)
        active = self.state.active_version(app_id)
        try:
            desired = self.remote.resolve(app_id, channel)
        except PackageDomainError as exc:  # registry/object store unreachable
            return UpdateOutcome(START_CACHED, active, error=str(exc)), None, active
        if desired is None:
            return UpdateOutcome(START_CACHED, active, error="no desired version"), None, active
        if desired.status == YANKED:
            return UpdateOutcome(SKIPPED_YANKED, active, target=desired.version), None, active
        if desired.version == active:
            return UpdateOutcome(START_ACTIVE, active), None, active
        if not force and self.state.is_failed(app_id, desired.version):
            return UpdateOutcome(SKIPPED_FAILED, active, target=desired.version), None, active
        if force:
            self.state.clear_failure(app_id, desired.version)
        return None, desired, active

    def execute_update(self, op: int, app_id: str, desired: Release, active: str | None) -> UpdateOutcome:
        """Run the download → verify → install → activate → observe transaction."""
        activated = False
        try:
            self.state.update_step(op, "DOWNLOADING")
            napp = self._download(app_id, desired)

            self.state.update_step(op, "VERIFYING")
            if _sha256_file(napp) != desired.sha256:
                raise HashMismatch(f"artifact sha256 != registry for {app_id}@{desired.version}")
            contents = verify_napp(napp, verifier=self.verifier)
            self._check_compatibility(contents.package)

            self.state.update_step(op, "EXTRACTING")
            staging_dir = self._versions_dir(app_id) / f"{desired.version}.staging"
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            install_source(napp, staging_dir, verifier=self.verifier)

            self.state.update_step(op, "DEPS_READY")
            pulled, reused_blobs = self._pull_blobs(contents.blob_references)
            fingerprint = contents.package["dependency_fingerprint"]
            venv_reused = self._prepare_venv(app_id, fingerprint, staging_dir)

            self.state.update_step(op, "MIGRATION_READY")
            if self._migrate is not None:
                self._migrate(staging_dir)

            self.state.update_step(op, "HEALTHCHECK")
            if not self._healthcheck(staging_dir):
                raise HealthcheckFailed("pre-start healthcheck failed")

            self.state.update_step(op, "ACTIVATING")
            final_dir = self._activate(app_id, desired.version, fingerprint, staging_dir)
            self.state.set_active(app_id, desired.version)
            activated = True

            self.state.update_step(op, "OBSERVING")
            if not self._observe(final_dir):
                self._revert(app_id, active)
                self.state.record_failure(app_id, desired.version, "observation failed")
                self.state.finish_operation(op, "rolled_back", "observation failed")
                return UpdateOutcome(ROLLED_BACK, self.state.active_version(app_id),
                                     target=desired.version, error="observation failed",
                                     blobs_pulled=pulled, blobs_reused=reused_blobs, venv_reused=venv_reused)

            self.state.set_last_known_good(app_id, desired.version)
            self.state.finish_operation(op, "succeeded")
            return UpdateOutcome(UPDATED, desired.version, target=desired.version,
                                 blobs_pulled=pulled, blobs_reused=reused_blobs, venv_reused=venv_reused)
        except OperationCancelled:
            # Cancel only fires before activation; no revert, and NOT recorded as
            # a failed version so the user can retry immediately.
            self.state.finish_operation(op, "cancelled", "cancelled by user")
            self._cleanup_staging(app_id, desired.version)
            return UpdateOutcome(CANCELLED, self.state.active_version(app_id),
                                 target=desired.version, error="cancelled")
        except Exception as exc:  # noqa: BLE001 - map every failure to a safe outcome
            self.state.record_failure(app_id, desired.version, f"{type(exc).__name__}: {exc}")
            self._cleanup_staging(app_id, desired.version)
            if activated:
                self._revert(app_id, active)
                self.state.finish_operation(op, "rolled_back", str(exc))
                return UpdateOutcome(ROLLED_BACK, self.state.active_version(app_id),
                                     target=desired.version, error=str(exc))
            self.state.finish_operation(op, "failed", str(exc))
            return UpdateOutcome(FAILED, self.state.active_version(app_id),
                                 target=desired.version, error=str(exc))

    def rollback(self, app_id: str) -> UpdateOutcome:
        """Manually revert to last-known-good (or previous active)."""
        target = self.state.last_known_good(app_id)
        current = self.state.active_version(app_id)
        if target is None or target == current:
            return UpdateOutcome(START_ACTIVE, current, error="no earlier version to roll back to")
        self._revert(app_id, target)
        return UpdateOutcome(ROLLED_BACK, self.state.active_version(app_id), target=target)

    def reconcile(self, app_id: str) -> list[UpdateOutcome]:
        """Repair operations interrupted by a crash / power loss (boot time)."""
        outcomes: list[UpdateOutcome] = []
        for op in self.state.running_operations(app_id):
            active_doc = self.read_active(op.app_id)
            final_dir = self._versions_dir(op.app_id) / op.to_version
            fully_installed = (
                final_dir.is_dir()
                and active_doc is not None
                and active_doc.get("version") == op.to_version
            )
            if fully_installed:
                # Activation had effectively completed; adopt it but do NOT assume
                # observation passed — leave LKG for the next healthy run.
                self.state.set_active(op.app_id, op.to_version)
                self.state.finish_operation(op.op_id, "succeeded", "reconciled: adopted activated version")
                outcomes.append(UpdateOutcome(UPDATED, op.to_version, target=op.to_version,
                                              details={"reconciled": True}))
            else:
                # Fail closed: revert to the previous active version, drop staging.
                self._cleanup_staging(op.app_id, op.to_version)
                self._revert(op.app_id, op.previous_active)
                self.state.finish_operation(op.op_id, "failed", "reconciled: incomplete update reverted")
                outcomes.append(UpdateOutcome(ROLLED_BACK, self.state.active_version(op.app_id),
                                              target=op.to_version, details={"reconciled": True}))
        return outcomes

    # ── steps ───────────────────────────────────────────────────────────────

    def _download(self, app_id: str, release: Release) -> Path:
        """Fetch the artifact with interruption resume (P2).

        Partial bytes persist in ``<version>.napp.part``; a retry continues from
        that offset (seek when the source supports it, skip-read otherwise)
        instead of re-transferring the whole package. The reassembled file must
        match the registry SHA-256 — a stale/corrupt ``.part`` triggers exactly
        one clean re-download before giving up.
        """
        staging = self._app_dir(app_id) / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        dest = staging / f"{release.version}.napp"
        part = staging / f"{release.version}.napp.part"
        for attempt in (1, 2):
            offset = part.stat().st_size if part.is_file() else 0
            if offset > release.size_bytes:
                part.unlink()  # bigger than the artifact: definitely stale
                offset = 0
            with self.remote.open_artifact(release) as source:
                if offset:
                    self._skip_to(source, offset)
                with part.open("ab") as target:
                    while chunk := source.read(_CHUNK):
                        target.write(chunk)
            if _sha256_file(part) == release.sha256:
                os.replace(part, dest)
                return dest
            part.unlink(missing_ok=True)  # stale resume data — retry once from zero
            if attempt == 2:
                raise HashMismatch(
                    f"downloaded artifact sha256 != registry for {release.app_id}@{release.version}"
                )
        raise AssertionError("unreachable")

    @staticmethod
    def _skip_to(source, offset: int) -> None:
        """Position ``source`` at ``offset``: seek when possible, else skip-read."""
        try:
            source.seek(offset)
            return
        except (AttributeError, OSError, ValueError):
            pass  # not seekable (e.g. a network stream) — fall back to skipping
        remaining = offset
        while remaining > 0:
            chunk = source.read(min(_CHUNK, remaining))
            if not chunk:
                raise ArtifactMissing("remote stream ended before the resume offset")
            remaining -= len(chunk)

    def _pull_blobs(self, references: list[dict]) -> tuple[int, int]:
        pulled = reused = 0
        for ref in references:
            digest = ref["sha256"]
            if self.blobs.has(digest):
                reused += 1
                continue
            try:
                source = self.remote_blobs.open(digest)
            except FileNotFoundError as exc:
                raise ArtifactMissing(f"blob missing on remote: {digest}") from exc
            with source:
                got, _ = self.blobs.put(source)
            if got != digest:
                raise HashMismatch(f"pulled blob does not match its address: {digest}")
            pulled += 1
        return pulled, reused

    def _prepare_venv(self, app_id: str, fingerprint: str, application_dir: Path | None = None) -> bool:
        """Make the runtime for ``fingerprint`` available; True = reused.

        Runtimes live in the GLOBAL store (``deps/runtimes/<fingerprint>``) so
        two apps with the same dependency lock share one venv (P1). Creation is
        race-safe without cross-process locks: build in a unique staging dir,
        rename into place; losing the rename race means someone else built the
        same fingerprint — adopt theirs once its ``.complete`` shows up.
        """
        global_dir = self.runtime_dir(fingerprint)
        if (global_dir / _VENV_COMPLETE).is_file():
            return True  # shared runtime already on this device
        if (self._legacy_venv_dir(app_id, fingerprint) / _VENV_COMPLETE).is_file():
            return True  # pre-convergence per-app venv, still valid — keep using it

        staging = self._runtimes_dir() / f".staging-{fingerprint}-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            if self._ensure_venv is not None:
                self._ensure_venv(fingerprint, staging)
            elif application_dir is not None:
                self._install_embedded_wheels(application_dir, staging)
            if self._warmup is not None:
                self._warmup(staging)
            (staging / _VENV_COMPLETE).write_text(
                json.dumps({"fingerprint": fingerprint, "completed_at": _utc_now()}), encoding="utf-8"
            )
            try:
                winfs.robust_rename(staging, global_dir)
            except FileExistsError:
                # Lost the race to a concurrent update installing the same
                # fingerprint. Theirs is as good as ours once complete.
                self._await_complete(global_dir)
                winfs.robust_rmtree(staging)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return False

    def _await_complete(self, runtime_dir: Path, timeout: float = 60.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if (runtime_dir / _VENV_COMPLETE).is_file():
                return
            time.sleep(0.2)
        raise ArtifactMissing(
            f"concurrent runtime install never completed: {runtime_dir.name} "
            "(刪掉該目錄後重試)"
        )

    @staticmethod
    def _install_embedded_wheels(application_dir: Path, venv_dir: Path) -> None:
        manifest_path = application_dir / "dependency-manifest.json"
        if not manifest_path.is_file():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        wheels = list(manifest.get("wheels") or [])
        if not wheels:
            return
        wheel_dir = application_dir / "wheels"
        for item in wheels:
            wheel = wheel_dir / item["name"]
            if not wheel.is_file():
                raise ArtifactMissing(f"embedded wheel missing: {item['name']}")
            if _sha256_file(wheel) != item["sha256"]:
                raise HashMismatch(f"embedded wheel hash mismatch: {item['name']}")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-index", "--find-links", str(wheel_dir),
             *list(manifest.get("requires") or [])],
            check=True,
        )

    def _check_compatibility(self, package: dict) -> None:
        if not self.expected_platform:
            return
        declared = package.get("platform", {})
        for key, want in self.expected_platform.items():
            got = declared.get(key)
            if got is not None and got != want:
                raise IncompatiblePackage(
                    f"platform {key}: package {got!r} incompatible with device {want!r}"
                )

    def _activate(self, app_id: str, version: str, fingerprint: str, staging_dir: Path) -> Path:
        final_dir = self._versions_dir(app_id) / version
        if final_dir.exists() and not winfs.robust_rmtree(final_dir):
            raise winfs.StillLocked(
                f"舊版本目錄被鎖住無法替換：{final_dir}", waited=0.0, last=None)
        # Same-filesystem rename, retried through transient Defender locks (P2).
        winfs.robust_rename(staging_dir, final_dir)
        # Sidecar meta records which venv this version needs, so GC can keep the
        # right venvs without re-reading the package.
        (self._versions_dir(app_id) / f"{version}{_META_SUFFIX}").write_text(
            json.dumps({"version": version, "dependency_fingerprint": fingerprint}),
            encoding="utf-8",
        )
        self._write_active(app_id, {
            "version": version,
            "path": str(final_dir),
            "dependency_fingerprint": fingerprint,
            "activated_at": _utc_now(),
        })
        return final_dir

    def _version_fingerprint(self, app_id: str, version: str) -> str | None:
        meta = self._versions_dir(app_id) / f"{version}{_META_SUFFIX}"
        if not meta.is_file():
            return None
        try:
            return json.loads(meta.read_text(encoding="utf-8")).get("dependency_fingerprint")
        except ValueError:
            return None

    def gc(self, app_id: str) -> dict:
        """Reclaim old version dirs and unreferenced runtimes; keep active + LKG.

        Blobs are content-addressed and shared, so they are left in place.
        Global runtimes (P1) are removed only when NO app on the device still
        references their fingerprint — the keep-set is computed across every
        app's remaining versions, not just this one's. Deletion failures are
        never swallowed (P2): what could not be removed is recorded in
        ``agent/deferred-gc.json``, reported in the result, and retried at the
        start of the next gc run.
        """
        deferred_cleared = self._retry_deferred()
        deferred: list[str] = []

        keep_versions = {v for v in (self.state.active_version(app_id),
                                     self.state.last_known_good(app_id)) if v}
        keep_fingerprints = {fp for v in keep_versions if (fp := self._version_fingerprint(app_id, v))}

        removed_versions: list[str] = []
        versions_dir = self._versions_dir(app_id)
        for child in versions_dir.iterdir() if versions_dir.is_dir() else []:
            if child.name.endswith(_META_SUFFIX) or child.name.endswith(".staging"):
                continue
            if child.is_dir() and child.name not in keep_versions:
                if winfs.robust_rmtree(child, attempts=3):
                    (versions_dir / f"{child.name}{_META_SUFFIX}").unlink(missing_ok=True)
                    removed_versions.append(child.name)
                else:
                    deferred.append(str(child))

        # Legacy per-app venvs: pre-convergence layout, same keep rule as before.
        removed_venvs: list[str] = []
        venvs_dir = self._app_dir(app_id) / "venvs"
        for child in venvs_dir.iterdir() if venvs_dir.is_dir() else []:
            if child.is_dir() and child.name not in keep_fingerprints:
                if winfs.robust_rmtree(child, attempts=3):
                    removed_venvs.append(child.name)
                else:
                    deferred.append(str(child))

        # Global runtime store: keep any fingerprint still referenced by ANY
        # app's remaining versions or active state (cross-app keep-set).
        removed_runtimes: list[str] = []
        device_keep = self._device_keep_fingerprints()
        runtimes_dir = self._runtimes_dir()
        for child in runtimes_dir.iterdir() if runtimes_dir.is_dir() else []:
            if not child.is_dir() or child.name.startswith(".staging-"):
                continue
            if child.name not in device_keep:
                if winfs.robust_rmtree(child, attempts=3):
                    removed_runtimes.append(child.name)
                else:
                    deferred.append(str(child))

        if deferred:
            self._record_deferred(deferred)
        return {"kept_versions": sorted(keep_versions),
                "removed_versions": sorted(removed_versions),
                "removed_venvs": sorted(removed_venvs),
                "removed_runtimes": sorted(removed_runtimes),
                "deferred": sorted(deferred),
                "deferred_cleared": sorted(deferred_cleared)}

    def _device_keep_fingerprints(self) -> set[str]:
        """Fingerprints referenced by any remaining version of any app.

        Conservative on purpose: a version dir that survives (even if not
        active) keeps its runtime — wrongly keeping a venv wastes disk, wrongly
        deleting one breaks another app.
        """
        keep: set[str] = set()
        apps_root = self.root / "applications"
        for app_dir in apps_root.iterdir() if apps_root.is_dir() else []:
            versions_dir = app_dir / "versions"
            for meta in versions_dir.glob(f"*{_META_SUFFIX}") if versions_dir.is_dir() else []:
                version = meta.name[: -len(_META_SUFFIX)]
                if (versions_dir / version).is_dir():
                    try:
                        fp = json.loads(meta.read_text(encoding="utf-8")).get("dependency_fingerprint")
                    except ValueError:
                        fp = None
                    if fp:
                        keep.add(fp)
        return keep

    # ── deferred cleanup (P2: GC failures are reported and retried, not lied about) ──

    def _deferred_path(self) -> Path:
        return self.root / "agent" / "deferred-gc.json"

    def _read_deferred(self) -> list[str]:
        path = self._deferred_path()
        if not path.is_file():
            return []
        try:
            return list(json.loads(path.read_text(encoding="utf-8")).get("paths", []))
        except ValueError:
            return []

    def _record_deferred(self, paths: list[str]) -> None:
        merged = sorted(set(self._read_deferred()) | set(paths))
        target = self._deferred_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"schema": 1, "paths": merged, "updated_at": _utc_now()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _retry_deferred(self) -> list[str]:
        pending = self._read_deferred()
        if not pending:
            return []
        cleared: list[str] = []
        still: list[str] = []
        for entry in pending:
            path = Path(entry)
            if not path.exists() or winfs.robust_rmtree(path, attempts=2):
                cleared.append(entry)
            else:
                still.append(entry)
        target = self._deferred_path()
        if still:
            target.write_text(
                json.dumps({"schema": 1, "paths": sorted(still), "updated_at": _utc_now()},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            target.unlink(missing_ok=True)
        return cleared

    def _write_active(self, app_id: str, doc: dict) -> None:
        path = self._active_json(app_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".active-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=2)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def _revert(self, app_id: str, version: str | None) -> None:
        """Point active back at ``version`` (or clear it when None)."""
        if version is None:
            self._active_json(app_id).unlink(missing_ok=True)
            self.state.set_active(app_id, None)
            return
        final_dir = self._versions_dir(app_id) / version
        if final_dir.is_dir():
            self._write_active(app_id, {
                "version": version,
                "path": str(final_dir),
                "activated_at": _utc_now(),
                "reverted": True,
            })
            self.state.set_active(app_id, version)
        else:  # target version no longer on disk — safest is no active app
            self._active_json(app_id).unlink(missing_ok=True)
            self.state.set_active(app_id, None)

    def _cleanup_staging(self, app_id: str, version: str) -> None:
        staging_dir = self._versions_dir(app_id) / f"{version}.staging"
        shutil.rmtree(staging_dir, ignore_errors=True)
        napp = self._app_dir(app_id) / "staging" / f"{version}.napp"
        napp.unlink(missing_ok=True)

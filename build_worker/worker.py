"""Build Worker core."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, build_napp, verify_napp
from provision_builder.napp.signing import Signer, Verifier
from provision_builder.package_services import PackageService, Release
from provision_builder.gateway import PlatformGateway, Target


class BuildCancelled(Exception):
    """Raised when a job observes its cancel event."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BuildLog:
    """Append-only structured (JSONL) build log with an in-memory mirror."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: list[dict] = []

    def event(self, step: str, message: str, level: str = "info", **extra) -> None:
        entry = {"ts": _utc_now(), "level": level, "step": step, "message": message, **extra}
        self.entries.append(entry)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def steps(self) -> list[str]:
        return [e["step"] for e in self.entries]


# Hook signatures: called with the job workspace dir; raise to fail the step.
Hook = Callable[[Path], None]


@dataclass
class BuildRequest:
    app_id: str
    version: str
    source_dir: Path            # already checked-out application source
    manifest: dict              # app.yaml/app.json content (already parsed)
    dependency_manifest: dict | None = None
    dependency_wheels_dir: Path | None = None
    dependency_fingerprint: str | None = None
    big_deps: dict[str, Path] = field(default_factory=dict)
    channel: str | None = None  # promote after publish when set
    source_commit: str = ""
    selfcheck: Hook | None = None    # offline resolve / import check
    healthcheck: Hook | None = None  # isolated apply/warmup + Tauri E2E in prod


@dataclass
class BuildResult:
    status: str                 # "succeeded" | "failed" | "cancelled"
    app_id: str
    version: str
    workspace: Path
    log_path: Path
    napp_path: Path | None = None
    canonical_digest: str = ""
    release: Release | None = None
    error: str | None = None


class BuildWorker:
    def __init__(
        self,
        service: PackageService,
        blobs: FileBlobStore,
        workspaces_root: Path | str,
        *,
        signer: Signer | None = None,
        verifier: Verifier | None = None,
        records=None,
        strict_dependencies: bool = False,
        require_production_healthcheck: bool = False,
        platform_gateway: PlatformGateway | None = None,
        dependency_target: Target | None = None,
    ):
        self.service = service
        self.blobs = blobs
        self.root = Path(workspaces_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.signer = signer
        self.verifier = verifier
        self.records = records  # optional BuildRecordStore
        self.strict_dependencies = strict_dependencies
        self.require_production_healthcheck = require_production_healthcheck
        self.platform_gateway = platform_gateway
        self.dependency_target = dependency_target or Target()
        self._counter = 0

    def _new_workspace(self, req: BuildRequest) -> Path:
        self._counter += 1
        ws = self.root / f"job-{req.app_id}-{req.version}-{self._counter}"
        if ws.exists():
            import shutil

            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        return ws

    @staticmethod
    def _check_cancel(cancel: threading.Event | None, log: BuildLog) -> None:
        if cancel is not None and cancel.is_set():
            log.event("cancel", "cancellation requested", level="warning")
            raise BuildCancelled()

    def run(self, req: BuildRequest, cancel: threading.Event | None = None) -> BuildResult:
        result = self._run(req, cancel)
        if self.records is not None:
            self.records.record(
                app_id=result.app_id, version=result.version, status=result.status,
                digest=result.canonical_digest, commit=req.source_commit,
                log_path=str(result.log_path), error=result.error,
            )
        return result

    def _run(self, req: BuildRequest, cancel: threading.Event | None = None) -> BuildResult:
        ws = self._new_workspace(req)
        log = BuildLog(ws / "build.jsonl")
        result = BuildResult("failed", req.app_id, req.version, ws, log.path)
        try:
            log.event("start", f"build {req.app_id}@{req.version}", commit=req.source_commit)

            self._check_cancel(cancel, log)
            log.event("validate", "validating app manifest")
            manifest = AppManifest.from_dict(req.manifest)
            if manifest.id != req.app_id or manifest.version != req.version:
                raise ValueError(
                    f"manifest {manifest.id}@{manifest.version} != request {req.app_id}@{req.version}"
                )
            if self.require_production_healthcheck and req.channel == "production" and not (manifest.healthcheck or req.healthcheck):
                raise ValueError("production promotion requires a real healthcheck")
            if manifest.requires and not req.dependency_manifest and self.platform_gateway is not None:
                log.event("dependencies", "resolving platform wheelhouse")
                pack_root = ws / "dependencies"
                req.dependency_manifest = self.platform_gateway.build_wheelhouse(
                    manifest.id, manifest.requires, pack_root, self.dependency_target
                )
                req.dependency_wheels_dir = pack_root / manifest.id / self.platform_gateway.wheels_dirname
                req.dependency_fingerprint = self.platform_gateway.requires_fingerprint(manifest.requires)
            if self.strict_dependencies and manifest.requires and not req.dependency_manifest:
                raise ValueError("requires is non-empty but no PlatformGateway dependency resolution was supplied")

            self._check_cancel(cancel, log)
            if req.selfcheck is not None:
                log.event("selfcheck", "offline dependency selfcheck")
                req.selfcheck(ws)

            self._check_cancel(cancel, log)
            log.event("build", "assembling .napp")
            napp_path = ws / f"{req.app_id}-{req.version}.napp"
            build = build_napp(
                manifest, req.source_dir, napp_path,
                dependency_manifest=req.dependency_manifest,
                dependency_wheels_dir=req.dependency_wheels_dir,
                dependency_fingerprint=req.dependency_fingerprint,
                big_deps=req.big_deps, blob_store=self.blobs,
                signer=self.signer, source_commit=req.source_commit, work_dir=ws,
            )
            result.napp_path = napp_path
            result.canonical_digest = build.canonical_digest
            log.event("build", "assembled", digest=build.canonical_digest,
                      source_files=build.package["artifact"]["source_files"],
                      blob_references=len(build.blob_references))

            self._check_cancel(cancel, log)
            log.event("verify", "verifying package integrity and signature")
            verify_napp(napp_path, verifier=self.verifier)

            self._check_cancel(cancel, log)
            if req.healthcheck is not None:
                log.event("healthcheck", "isolated apply/warmup + E2E")
                req.healthcheck(ws)

            self._check_cancel(cancel, log)
            log.event("publish", "uploading artifact and registering release")
            release = self.service.publish(req.app_id, req.version, napp_path)
            result.release = release

            if req.channel:
                log.event("promote", f"promoting to {req.channel}")
                self.service.promote(req.app_id, req.channel, req.version)

            result.status = "succeeded"
            log.event("done", "build succeeded")
            return result
        except BuildCancelled:
            result.status = "cancelled"
            result.error = "cancelled"
            log.event("done", "build cancelled", level="warning")
            return result
        except Exception as exc:  # keep the log + workspace for diagnosis
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            log.event("error", result.error, level="error")
            return result


def run_subprocess(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    log: BuildLog | None = None,
    cancel: threading.Event | None = None,
    timeout: float | None = None,
    poll: float = 0.1,
) -> int:
    """Run a subprocess, killing the whole process tree on cancel or timeout.

    Used for the real checkout / selfcheck / apply / warmup / E2E steps. Returns
    the exit code; raises BuildCancelled if the cancel event fires first.
    """
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    popen_kwargs = {"cwd": str(cwd) if cwd else None, "creationflags": creationflags}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True  # own process group for group-kill
    proc = subprocess.Popen(cmd, **popen_kwargs)
    if log is not None:
        log.event("subprocess", "started " + " ".join(cmd), pid=proc.pid)
    waited = 0.0
    try:
        while True:
            try:
                return proc.wait(timeout=poll)
            except subprocess.TimeoutExpired:
                waited += poll
                if cancel is not None and cancel.is_set():
                    _kill_tree(proc)
                    raise BuildCancelled()
                if timeout is not None and waited >= timeout:
                    _kill_tree(proc)
                    raise TimeoutError(f"subprocess exceeded {timeout}s: {' '.join(cmd)}")
    finally:
        if proc.poll() is None:
            _kill_tree(proc)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

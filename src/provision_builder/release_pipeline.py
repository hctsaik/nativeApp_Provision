"""Release pipeline (P0): the single machine-checkable "what ships" contract.

Assembles one immutable ``release/<release-id>/`` tree from explicitly named
inputs — never from a workspace scan — and refuses to build when any input
smells like development residue (`_run`, `__pycache__`, WebView profiles, …).
``dist/`` and every other build workspace stay deletable; only a directory
produced (and verified) by this module is a deliverable.

Output layout (docs/NATIVEAPP_DEPLOYMENT_RECOMMENDATION.md §6):

    release/<release-id>/
    ├─ CIM-Setup-*.exe            optional, built on a non-WDAC machine
    ├─ offline-channel/
    │  ├─ channel.json            FileChannelRemote-compatible (native_agent)
    │  ├─ artifacts/*.napp
    │  └─ blobs/sha256/<hash>
    ├─ extras/<name>/             optional gate-scanned payload directories
    ├─ release-manifest.json
    ├─ SBOM.json
    ├─ RELEASE-REPORT.md
    └─ checksums.sha256           written last; covers every other file

The ``offline-channel`` deliberately reuses the ``channel.json`` schema of
:mod:`native_agent.file_remote` so a release can be pointed at directly as an
update source — no third format.

Stdlib-only (SPEC D2). Signature policy: the ``production`` channel refuses
unsigned or unverifiable ``.napp`` artifacts; other channels verify integrity
only (but a *present* signature must still verify when keys are given — a bad
signature is never acceptable).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from provision_builder._util import human_size, sha256_file
from provision_builder.blob_store import FileBlobStore
from provision_builder.device_payload import TRUST_STORE_NAME, write_device_tools
from provision_builder.napp.errors import SignatureInvalid
from provision_builder.napp.reader import NappContents, verify_napp
from provision_builder.napp.signing import Verifier

SCHEMA = 1
BUILDER_VERSION = "release-pipeline/1"
PRODUCTION = "production"

CHANNEL_DIR = "offline-channel"
ARTIFACTS_DIR = "artifacts"
EXTRAS_DIR = "extras"
MANIFEST_NAME = "release-manifest.json"
SBOM_NAME = "SBOM.json"
REPORT_NAME = "RELEASE-REPORT.md"
CHECKSUMS_NAME = "checksums.sha256"
CHANNEL_INDEX = "channel.json"  # keep in sync with native_agent.file_remote.INDEX

# ---------------------------------------------------------------------------
# Anti-mispackaging gate. Names that can never be legitimate release payload.
# The gate REJECTS (fails the build listing the paths) instead of silently
# excluding: a silently-thinned package looks complete until the factory floor.
# ---------------------------------------------------------------------------
REJECT_DIRS_ANY_DEPTH = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".venv", "_run", "wv2", ".wv2",
}
# Junk at a payload root; one level down the same name can be real data
# (the `dist/` lesson: a Streamlit component's frontend/dist IS the component).
REJECT_DIRS_ROOT = {
    "venv", "env", "e2e", "logs", "log", "cache", "tmp", "temp",
    "dist", "build", "staging", "data", ".local-services",
}
REJECT_FILE_SUFFIXES = {".pyc", ".pyo", ".log"}
REJECT_FILE_NAMES = {".coverage", "Thumbs.db"}

_MAX_LISTED_VIOLATIONS = 20
_RELEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CHANNEL_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")


class ReleaseError(Exception):
    """The release cannot be built or verified; the message says what to fix."""


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def scan_payload_tree(root: Path) -> list[str]:
    """Return gate violations (relative paths + reason) under ``root``.

    Flagged directories are pruned so one offending ``__pycache__`` yields one
    line, not four thousand.
    """
    root = Path(root)
    problems: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        rel_dir = current.relative_to(root)
        keep: list[str] = []
        for name in sorted(dirnames):
            rel = rel_dir / name
            if name in REJECT_DIRS_ANY_DEPTH:
                problems.append(f"{rel} （禁止的目錄名，任何深度）")
            elif current == root and name.lower() in REJECT_DIRS_ROOT:
                problems.append(f"{rel} （payload 根層禁止的目錄名）")
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = rel_dir / name
            if name in REJECT_FILE_NAMES:
                problems.append(f"{rel} （禁止的檔名）")
            elif Path(name).suffix.lower() in REJECT_FILE_SUFFIXES:
                problems.append(f"{rel} （禁止的副檔名）")
    return problems


def _raise_gate(source: str, problems: list[str]) -> None:
    listed = problems[:_MAX_LISTED_VIOLATIONS]
    more = len(problems) - len(listed)
    lines = "\n".join(f"  - {p}" for p in listed)
    if more > 0:
        lines += f"\n  …另 {more} 項"
    raise ReleaseError(
        f"防誤包 gate 拒絕 {source}（{len(problems)} 項開發殘留；"
        f"清掉或改用乾淨的輸出來源）：\n{lines}"
    )


# ---------------------------------------------------------------------------
# Inspection results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtifactInfo:
    app_id: str
    version: str
    source: Path                 # input .napp path
    file: str                    # posix relpath inside the release
    sha256: str
    size: int
    dependency_fingerprint: str
    signed: bool
    key_id: str | None
    requires: list[str] = field(default_factory=list)
    wheels: list[str] = field(default_factory=list)
    blob_refs: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ReleaseResult:
    path: Path
    release_id: str
    channel: str
    manifest: dict


def _read_dependency_manifest(napp_path: Path) -> dict:
    with zipfile.ZipFile(napp_path) as zf:
        try:
            return json.loads(zf.read("dependency-manifest.json").decode("utf-8"))
        except KeyError:
            return {}


def _inspect_napp(path: Path, *, channel: str, verifier: Verifier | None) -> ArtifactInfo:
    if not path.is_file():
        raise ReleaseError(f"找不到 .napp：{path}")
    contents: NappContents = verify_napp(path)  # integrity: checksums + no smuggled files
    package = contents.package
    app_id, version = package["app_id"], package["version"]

    signature = contents.signature
    if channel == PRODUCTION:
        if verifier is None:
            raise ReleaseError(
                "production channel 需要信任金鑰（--trust key_id:secret）才能驗章；"
                "沒有金鑰就不能出 production release"
            )
        if signature is None:
            raise ReleaseError(f"production channel 拒絕未簽章的 artifact：{path.name}（{app_id}@{version}）")
    if signature is not None and verifier is not None:
        try:
            verifier.verify(contents.canonical_digest, signature)
        except SignatureInvalid as exc:
            raise ReleaseError(f"{path.name}（{app_id}@{version}）簽章驗證失敗：{exc}") from exc

    dep = _read_dependency_manifest(path)
    digest, size = sha256_file(path)
    return ArtifactInfo(
        app_id=app_id,
        version=version,
        source=path,
        file=f"{CHANNEL_DIR}/{ARTIFACTS_DIR}/{app_id}-{version}.napp",
        sha256=digest,
        size=size,
        dependency_fingerprint=package.get("dependency_fingerprint", ""),
        signed=signature is not None,
        key_id=signature.key_id if signature is not None else None,
        requires=list(dep.get("requires", [])),
        wheels=[w.get("name", "") for w in dep.get("wheels", [])],
        blob_refs=list(contents.blob_references),
    )


def _collect_blobs(artifacts: list[ArtifactInfo], blob_root: Path | None) -> list[dict]:
    used_by: dict[str, list[str]] = {}
    sizes: dict[str, int] = {}
    for art in artifacts:
        for ref in art.blob_refs:
            digest = ref["sha256"]
            used_by.setdefault(digest, []).append(art.app_id)
            sizes[digest] = ref.get("size", 0)
    if not used_by:
        return []
    if blob_root is None:
        needed = ", ".join(sorted(used_by))
        raise ReleaseError(f"artifact 引用了 blob 但未指定 --blobs 來源。缺少：{needed}")
    store = FileBlobStore(blob_root)
    blobs: list[dict] = []
    for digest in sorted(used_by):
        try:
            store.verify(digest)
        except Exception as exc:
            apps = ", ".join(sorted(set(used_by[digest])))
            raise ReleaseError(
                f"blob {digest[:16]}… 缺失或損壞（{exc}）；被 {apps} 引用。"
                f"把正確的 blob 放回 {store.prefix} 再重跑"
            ) from exc
        blobs.append({
            "sha256": digest,
            "size": sizes[digest],
            "used_by": sorted(set(used_by[digest])),
        })
    return blobs


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_release(
    out_parent: Path | str,
    napp_paths: Sequence[Path | str],
    *,
    channel: str = "internal",
    release_id: str | None = None,
    blob_root: Path | str | None = None,
    setup_exe: Path | str | None = None,
    extras: dict[str, Path | str] | None = None,
    verifier: Verifier | None = None,
    promoted_from: str | None = None,
    trust_store_file: Path | str | None = None,
) -> ReleaseResult:
    """Assemble a fresh ``<out_parent>/<release-id>/`` from explicit inputs.

    Everything is validated *before* the first write; assembly happens in a
    staging directory renamed into place at the end, so a crash never leaves a
    half directory that looks like a release.
    """
    if not napp_paths:
        raise ReleaseError("release 至少要有一個 .napp artifact")
    if not _CHANNEL_RE.fullmatch(channel):
        raise ReleaseError(f"channel 名稱不合法：{channel!r}")
    release_id = release_id or f"{channel}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    if not _RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseError(f"release id 不合法：{release_id!r}")

    out_parent = Path(out_parent)
    out_dir = out_parent / release_id
    if out_dir.exists():
        raise ReleaseError(
            f"輸出目錄已存在：{out_dir}。release 一律用全新目錄，"
            "不從歷史輸出就地增補（換一個 release id 或刪掉舊目錄）"
        )

    # ---- inspect（零寫入）----
    artifacts = [_inspect_napp(Path(p), channel=channel, verifier=verifier) for p in napp_paths]
    seen: dict[str, str] = {}
    for art in artifacts:
        if art.app_id in seen:
            raise ReleaseError(
                f"同一個 release 內有重複的 app：{art.app_id}"
                f"（{seen[art.app_id]} 與 {art.version}）。一個 channel 索引每個 app 只有一個版本"
            )
        seen[art.app_id] = art.version

    blobs = _collect_blobs(artifacts, Path(blob_root) if blob_root else None)

    setup_path = Path(setup_exe) if setup_exe else None
    if setup_path is not None and not setup_path.is_file():
        raise ReleaseError(f"找不到 setup 安裝檔：{setup_path}")

    extras = {name: Path(path) for name, path in (extras or {}).items()}
    for name, path in extras.items():
        if not _RELEASE_ID_RE.fullmatch(name):
            raise ReleaseError(f"extra 名稱不合法：{name!r}")
        if not path.is_dir():
            raise ReleaseError(f"extra 目錄不存在：{name}={path}")
        problems = scan_payload_tree(path)
        if problems:
            _raise_gate(f"extra '{name}'（{path}）", problems)

    # ---- assemble（staging → 原子換位）----
    staging = out_parent / f".release-staging-{release_id}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        art_dir = staging / CHANNEL_DIR / ARTIFACTS_DIR
        art_dir.mkdir(parents=True)
        channel_entries: list[dict] = []
        for art in artifacts:
            target = staging / Path(art.file)
            shutil.copyfile(art.source, target)
            copied_digest, copied_size = sha256_file(target)
            if copied_digest != art.sha256:
                raise ReleaseError(f"複製後雜湊不符（來源被同時修改？）：{art.source}")
            # 與 native_agent.file_remote.FileChannelRemote 相容的 Release 形狀
            channel_entries.append({
                "app_id": art.app_id,
                "version": art.version,
                "object_key": f"applications/{art.app_id}/{art.version}/{art.app_id}-{art.version}.napp",
                "sha256": art.sha256,
                "size_bytes": copied_size,
                "status": "published",
                "created_at": _utc_now(),
                "artifact": f"{ARTIFACTS_DIR}/{art.app_id}-{art.version}.napp",
            })
        (staging / CHANNEL_DIR / CHANNEL_INDEX).write_text(
            json.dumps({"schema": 1, "channel": channel, "releases": channel_entries}, indent=2),
            encoding="utf-8",
        )

        if blobs:
            store = FileBlobStore(Path(blob_root))
            for blob in blobs:
                store.link_into(blob["sha256"], staging / CHANNEL_DIR / "blobs" / "sha256" / blob["sha256"])

        setup_info = None
        if setup_path is not None:
            shutil.copyfile(setup_path, staging / setup_path.name)
            digest, size = sha256_file(staging / setup_path.name)
            setup_info = {"file": setup_path.name, "sha256": digest, "size": size}

        for name, path in extras.items():
            shutil.copytree(path, staging / EXTRAS_DIR / name)

        # 平台 release 自帶裝置端安裝器（install.bat + tools/）與發行者信任清單：
        # 目標機雙擊即安裝，首次安裝時把清單釘住（TOFU），之後更新驗釘住的那份。
        if any(a.app_id == "cim-platform" for a in artifacts):
            write_device_tools(staging, trust_store_file)

        # Payload totals（解壓後磁碟需求）：此刻樹上只有 payload，
        # manifest/SBOM/報告/checksums 尚未寫入，統計因此穩定且不自我引用。
        payload_files = 0
        payload_bytes = 0
        for path in _iter_release_files(staging):
            payload_files += 1
            payload_bytes += path.stat().st_size

        manifest = {
            "schema": SCHEMA,
            "builder_version": BUILDER_VERSION,
            "release_id": release_id,
            "channel": channel,
            "created_at": _utc_now(),
            "artifacts": [
                {
                    "app_id": a.app_id, "version": a.version, "file": a.file,
                    "sha256": a.sha256, "size": a.size,
                    "dependency_fingerprint": a.dependency_fingerprint,
                    "signed": a.signed, "key_id": a.key_id,
                }
                for a in artifacts
            ],
            "blobs": blobs,
            "setup": setup_info,
            "extras": sorted(extras),
            "promoted_from": promoted_from,
            "totals": {"files": payload_files, "bytes": payload_bytes, "scope": "payload"},
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (staging / SBOM_NAME).write_text(
            json.dumps(_sbom(release_id, artifacts, blobs), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        _write_checksums_and_report(staging, manifest, artifacts, blobs, setup_info)

        staging.rename(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return ReleaseResult(out_dir, release_id, channel, manifest)


def _sbom(release_id: str, artifacts: list[ArtifactInfo], blobs: list[dict]) -> dict:
    """Honest minimal SBOM: what each app declares, nothing invented."""
    return {
        "schema": 1,
        "scope": "python-dependencies-as-declared",
        "release_id": release_id,
        "apps": [
            {
                "app_id": a.app_id,
                "version": a.version,
                "dependency_fingerprint": a.dependency_fingerprint,
                "requires": a.requires,
                "wheels": a.wheels,
                "blobs": [ref["sha256"] for ref in a.blob_refs],
            }
            for a in artifacts
        ],
        "blobs": blobs,
    }


def _iter_release_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _write_checksums_and_report(
    root: Path,
    manifest: dict,
    artifacts: list[ArtifactInfo],
    blobs: list[dict],
    setup_info: dict | None,
) -> None:
    """Write RELEASE-REPORT.md, then checksums.sha256 covering everything else."""
    art_rows = "\n".join(
        f"| {a.app_id} | {a.version} | {human_size(a.size)} | "
        f"{'已簽章（' + str(a.key_id) + '）' if a.signed else '未簽章'} | `{a.sha256[:16]}…` |"
        for a in artifacts
    )
    blob_rows = "\n".join(
        f"| `{b['sha256'][:16]}…` | {human_size(b['size'])} | {', '.join(b['used_by'])} |"
        for b in blobs
    ) or "| （無） | | |"
    setup_line = (
        f"- 安裝檔：`{setup_info['file']}`（{human_size(setup_info['size'])}）"
        if setup_info else "- 安裝檔：本次未附（純離線通道更新包）"
    )
    if (root / "install.bat").is_file():
        usage = (f"1. 整個 `{manifest['release_id']}\\` 資料夾複製到目標機（USB 可）。\n"
                 "2. 目標機**雙擊 `install.bat`**——首次＝安裝（會釘住發行者信任清單），\n"
                 "   之後拿新的 release 資料夾再雙擊同一顆＝更新（使用者資料不動）。\n"
                 "3. 啟動：安裝根目錄的 `bin\\start-platform.bat`"
                 "（預設 `%LOCALAPPDATA%\\CIM-Platform`）。")
    else:
        usage = (f"1. 整個 `{manifest['release_id']}\\` 資料夾複製到目標機（USB 可）。\n"
                 "2. 先驗證完整性：`py -3.11 release.py verify <此資料夾>`。\n"
                 "3. 把 `offline-channel\\` 設為 App 的 update source（`config.json` 的\n"
                 "   `update_source`），或用 Native Agent 直接指向它——`channel.json`\n"
                 "   即為通道索引，Agent 只下載缺少的內容。")
    report = f"""# Release 報告 — {manifest['release_id']}

- 產生時間：{manifest['created_at']}
- Channel：**{manifest['channel']}**
- Builder：{manifest['builder_version']}
- 內容：{manifest['totals']['files']} 個檔案、共 {human_size(manifest['totals']['bytes'])}（解壓後磁碟需求，不含本報告與檢查檔）
{setup_line}

## Artifacts

| App | 版本 | 大小 | 簽章 | SHA-256 |
|-----|------|------|------|---------|
{art_rows}

## 大型內容（blobs，內容定址）

| SHA-256 | 大小 | 引用者 |
|---------|------|--------|
{blob_rows}

## 離線機使用方式

{usage}

## 注意

- 本資料夾不可就地修改；要出新版就產生新的 release 目錄。
- `checksums.sha256` 覆蓋本資料夾除它自身外的每一個檔案；
  多出、缺少或被改動的檔案都會讓 verify 失敗。
"""
    (root / REPORT_NAME).write_text(report, encoding="utf-8")

    lines: list[str] = []
    for path in _iter_release_files(root):
        rel = path.relative_to(root).as_posix()
        if rel == CHECKSUMS_NAME:
            continue
        digest, _size = sha256_file(path)
        lines.append(f"{digest}  {rel}")
    (root / CHECKSUMS_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Promote（internal → pilot → production：同一批 bytes，換通道重驗重組）
# ---------------------------------------------------------------------------

def promote_release(
    source_dir: Path | str,
    out_parent: Path | str,
    *,
    to_channel: str,
    release_id: str | None = None,
    verifier: Verifier | None = None,
) -> ReleaseResult:
    """Re-cut an existing release into a stricter channel.

    Promotion never rebuilds artifacts — the same ``.napp`` bytes flow through
    ``build_release`` again under the target channel's policy, so promoting to
    ``production`` re-runs every gate including signature verification. The new
    manifest records ``promoted_from`` for provenance.
    """
    source = Path(source_dir)
    problems = verify_release(source, verifier=verifier)
    # 來源是較寬鬆通道時，「production 需要金鑰」不算來源的錯——目標通道會強制。
    blocking = [p for p in problems if "信任金鑰" not in p]
    if blocking:
        listed = "\n".join(f"  - {p}" for p in blocking[:_MAX_LISTED_VIOLATIONS])
        raise ReleaseError(f"來源 release 驗證未過，不能晉升：\n{listed}")
    manifest = json.loads((source / MANIFEST_NAME).read_text(encoding="utf-8"))

    napp_paths = [source / Path(art["file"]) for art in manifest.get("artifacts", [])]
    blob_root = source / CHANNEL_DIR / "blobs"
    extras = {name: source / EXTRAS_DIR / name for name in manifest.get("extras", [])}
    setup = manifest.get("setup")
    setup_path = source / setup["file"] if setup else None

    shipped_trust = source / TRUST_STORE_NAME
    return build_release(
        out_parent,
        napp_paths,
        channel=to_channel,
        release_id=release_id,
        blob_root=blob_root if manifest.get("blobs") else None,
        setup_exe=setup_path,
        extras=extras,
        verifier=verifier,
        promoted_from=manifest.get("release_id"),
        trust_store_file=shipped_trust if shipped_trust.is_file() else None,
    )


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify_release(release_dir: Path | str, *, verifier: Verifier | None = None) -> list[str]:
    """Re-check a release tree; return a list of problems (empty = deliverable).

    This is the machine-checkable answer to「這個資料夾可以出貨嗎」：
    逐檔雜湊、不許有未列入 manifest 的檔案、.napp 完整性與（production）簽章、
    channel.json 一致性、blob 存在且雜湊正確。
    """
    root = Path(release_dir)
    problems: list[str] = []

    manifest_path = root / MANIFEST_NAME
    checksums_path = root / CHECKSUMS_NAME
    if not manifest_path.is_file():
        return [f"缺 {MANIFEST_NAME}——這不是 release 目錄（build workspace 不是交付物）"]
    if not checksums_path.is_file():
        return [f"缺 {CHECKSUMS_NAME}——release 未完成或被動過"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    channel = manifest.get("channel", "")

    # 1) 逐檔雜湊 + 未列入/缺少檔案
    listed: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, _, rel = line.partition("  ")
            listed[rel] = digest
    on_disk = {
        p.relative_to(root).as_posix()
        for p in _iter_release_files(root)
        if p.relative_to(root).as_posix() != CHECKSUMS_NAME
    }
    for rel in sorted(on_disk - set(listed)):
        problems.append(f"未列入 manifest 的檔案：{rel}")
    for rel in sorted(set(listed) - on_disk):
        problems.append(f"checksums 列出的檔案缺失：{rel}")
    for rel in sorted(on_disk & set(listed)):
        digest, _ = sha256_file(root / rel)
        if digest != listed[rel]:
            problems.append(f"雜湊不符：{rel}")

    # 2) .napp 完整性與簽章政策
    for art in manifest.get("artifacts", []):
        napp = root / Path(art["file"])
        if not napp.is_file():
            problems.append(f"artifact 缺失：{art['file']}")
            continue
        try:
            contents = verify_napp(napp)
        except Exception as exc:
            problems.append(f"artifact 驗證失敗：{art['file']}（{exc}）")
            continue
        if channel == PRODUCTION:
            if contents.signature is None:
                problems.append(f"production artifact 未簽章：{art['file']}")
            elif verifier is None:
                problems.append("production release 需要信任金鑰才能完整驗證（--trust key_id:secret）")
            else:
                try:
                    verifier.verify(contents.canonical_digest, contents.signature)
                except SignatureInvalid as exc:
                    problems.append(f"簽章驗證失敗：{art['file']}（{exc}）")

    # 3) channel.json 與 manifest 一致
    index_path = root / CHANNEL_DIR / CHANNEL_INDEX
    if not index_path.is_file():
        problems.append(f"缺 {CHANNEL_DIR}/{CHANNEL_INDEX}")
    else:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        by_app = {a["app_id"]: a for a in manifest.get("artifacts", [])}
        for item in index.get("releases", []):
            expected = by_app.get(item["app_id"])
            if expected is None:
                problems.append(f"channel.json 有 manifest 沒有的 app：{item['app_id']}")
            elif item["sha256"] != expected["sha256"] or item["version"] != expected["version"]:
                problems.append(f"channel.json 與 manifest 不一致：{item['app_id']}")
        if index.get("channel") != channel:
            problems.append("channel.json 的 channel 與 manifest 不一致")

    # 4) blobs 存在且內容正確
    if manifest.get("blobs"):
        store = FileBlobStore(root / CHANNEL_DIR / "blobs")
        for blob in manifest["blobs"]:
            try:
                store.verify(blob["sha256"])
            except Exception as exc:
                apps = ", ".join(blob.get("used_by", []))
                problems.append(f"blob 缺失或損壞：{blob['sha256'][:16]}…（{exc}；引用者：{apps}）")

    return problems

"""Publisher-signature verification for the folder update channel (P3.2).

files.json proves the bytes are intact; it says nothing about who produced
them. An attacker who can write to the update source can regenerate a
self-consistent payload + files.json + release.json. This module closes that
hole: the publisher signs the *canonical digest of files.json* on the build
machine, and the device verifies it against an admin-installed trust store
before anything becomes installable.

Policy (read from ``apps/<app>/config.json`` + ``trusted_publishers.json``):

- ``require_signed_updates: true`` → every staged version MUST carry a valid
  signature from a trusted key; missing trust store is a configuration error.
- trust store present, not required → an unsigned version is still accepted
  (opt-in migration), but a PRESENT signature must verify — a bad or untrusted
  signature is never acceptable.
- neither configured → no-op (pre-signature devices keep working).

``signature.json`` lives in the version dir, integrity-exempt like
``.complete`` (it cannot be listed in the manifest it signs), and travels with
exports automatically. Stdlib-only; Ed25519 comes from the pure-Python RFC 8032
module shipped alongside the bootstrap (``ed25519.py``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

try:  # in-repo (package) form
    from provision_builder.napp import ed25519
except ImportError:  # deployed form: bootstrap/ed25519.py sits next to us
    import ed25519  # type: ignore[no-redef]

if __package__:
    from . import integrity
else:
    import integrity  # type: ignore[no-redef]

SIGNATURE_NAME = "signature.json"
TRUST_STORE_NAME = "trusted_publishers.json"
CONFIG_KEY = "require_signed_updates"
ALGORITHM = "ed25519"


class SignaturePolicyError(Exception):
    """The version cannot be accepted under the device's signature policy."""


def version_digest(files_manifest: dict) -> str:
    """Canonical digest of a files.json — what the publisher signature commits to.

    Same construction as the ``.napp`` canonical digest: sha256 over the
    ``{path: sha256}`` map serialised with sorted keys and tight separators.
    Signing the digest of the manifest (whose per-file hashes the device has
    already verified byte-for-byte) extends the signature to every payload byte.
    """
    mapping = {entry["path"]: entry["sha256"] for entry in files_manifest.get("files", [])}
    payload = json.dumps(mapping, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_trust_store(path: Path) -> dict[str, bytes]:
    """{key_id: ed25519 public key}. ``retired`` keys still verify (they signed
    valid old releases); revocation = the entry is gone from the file."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SignaturePolicyError(f"信任清單讀不了:{path}({exc})") from exc
    entries = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not entries:
        raise SignaturePolicyError(f"信任清單沒有任何金鑰:{path}")
    keys: dict[str, bytes] = {}
    for entry in entries:
        try:
            if entry.get("algorithm") != ALGORITHM:
                raise SignaturePolicyError(
                    f"信任清單內有不支援的演算法:{entry.get('algorithm')!r}")
            keys[str(entry["key_id"])] = bytes.fromhex(entry["public_key"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SignaturePolicyError(f"信任清單項目格式不對:{entry!r}({exc})") from exc
    return keys


def check_version_signature(version_dir: Path, *, config: dict,
                            trust_path: Path) -> str:
    """Enforce the signature policy on a staged/installed version directory.

    Returns an outcome tag (``verified`` / ``unsigned-allowed`` /
    ``not-configured``); raises :class:`SignaturePolicyError` with an
    operator-actionable message otherwise.
    """
    version_dir = Path(version_dir)
    require = bool(config.get(CONFIG_KEY, False))
    trust_exists = Path(trust_path).is_file()
    signature_path = version_dir / SIGNATURE_NAME

    if not require and not trust_exists:
        return "not-configured"  # 簽章政策未啟用(向後相容)

    if require and not trust_exists:
        raise SignaturePolicyError(
            f"config.json 要求已簽章更新({CONFIG_KEY}: true),但找不到信任清單。\n"
            f"  把發行者公鑰清單放到:{trust_path}")

    if not signature_path.is_file():
        if require:
            raise SignaturePolicyError(
                "這個版本沒有發行者簽章(缺 signature.json),而這台機器要求已簽章更新。\n"
                "  請發布人員用 release.py sign-version 對版本簽章後重新匯出。")
        return "unsigned-allowed"  # 信任清單在、未強制:未簽章版本仍放行(過渡期)

    try:
        bundle = json.loads(signature_path.read_text(encoding="utf-8"))
        algorithm = bundle["algorithm"]
        key_id = str(bundle["key_id"])
        declared_digest = bundle["canonical_digest"]
        signature = bytes.fromhex(bundle["signature"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SignaturePolicyError(f"signature.json 格式不對({exc})") from exc

    if algorithm != ALGORITHM:
        raise SignaturePolicyError(f"不支援的簽章演算法:{algorithm!r}")

    digest = version_digest(integrity.load_files_json(version_dir))
    if declared_digest != digest:
        raise SignaturePolicyError(
            "簽章對象與 files.json 不符 — 檔案清單在簽章之後被改過。\n"
            "  這正是簽章要擋的事;不要安裝這個更新,並檢查更新來源是否被動過手腳。")

    keys = load_trust_store(trust_path)
    public = keys.get(key_id)
    if public is None:
        raise SignaturePolicyError(
            f"簽章的 key_id={key_id!r} 不在這台機器的信任清單內。\n"
            f"  信任的 key_id:{sorted(keys)}。若這是新發行金鑰,請管理員更新 {TRUST_STORE_NAME}。")

    if not ed25519.verify(public, digest.encode("ascii"), signature):
        raise SignaturePolicyError(
            f"發行者簽章驗證失敗(key_id={key_id})。\n"
            "  版本內容與簽章不匹配;不要安裝這個更新,並檢查更新來源。")
    return "verified"

"""Publisher keys and the device trust-store (ADR 0001 §3–4).

- A *private key file* stays on the build/signing machine (secret store).
- A *trust store* is the device-side list of trusted public keys
  (key_id → Ed25519 public key). ``retired: true`` keys stop signing new
  packages but still verify old ones; revocation = remove the entry.
- ``sign_napp`` adds a detached signature to an already-built ``.napp``
  without touching its payload — ``signature.json`` is checksum-exempt by the
  format, and the canonical digest it commits to is recomputed, not trusted.

Stdlib-only.
"""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from provision_builder.napp import _layout as L
from provision_builder.napp import ed25519
from provision_builder.napp.errors import InvalidManifest, SignatureInvalid
from provision_builder.napp.signing import (
    ED25519_ALGORITHM,
    Ed25519Signer,
    Ed25519Verifier,
    MultiKeyVerifier,
    SignatureBundle,
    Verifier,
    sign_digest,
)

TRUST_SCHEMA = 1


class TrustStoreError(Exception):
    """The trust store / key file cannot be used; message says what to fix."""


def generate_keypair(key_id: str) -> dict:
    """A fresh publisher keypair as a private-key document (keep it secret)."""
    seed = os.urandom(ed25519.SEED_BYTES)
    return {
        "schema": TRUST_SCHEMA,
        "algorithm": ED25519_ALGORITHM,
        "key_id": key_id,
        "private_seed": seed.hex(),
        "public_key": ed25519.secret_to_public(seed).hex(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def load_private_key(path: Path | str) -> Ed25519Signer:
    doc = _read_json(Path(path), "private key")
    if doc.get("algorithm") != ED25519_ALGORITHM:
        raise TrustStoreError(f"不支援的簽章演算法：{doc.get('algorithm')!r}")
    try:
        seed = bytes.fromhex(doc["private_seed"])
        signer = Ed25519Signer(seed, doc["key_id"])
    except (KeyError, ValueError) as exc:
        raise TrustStoreError(f"私鑰檔格式不對：{path}（{exc}）") from exc
    declared = doc.get("public_key")
    if declared and declared != signer.public_key.hex():
        raise TrustStoreError(f"私鑰檔的 public_key 與私鑰不符：{path}（檔案被改過？）")
    return signer


def trust_entry(signer: Ed25519Signer, *, retired: bool = False) -> dict:
    """The public half of ``signer`` in trust-store entry form."""
    return {
        "key_id": signer.key_id,
        "algorithm": ED25519_ALGORITHM,
        "public_key": signer.public_key.hex(),
        "retired": retired,
    }


def load_trust_store(path: Path | str,
                     extra: dict[str, Verifier] | None = None) -> MultiKeyVerifier:
    """Build the device verifier from a trust-store file.

    ``retired`` keys still verify (they signed valid old packages); a key_id
    absent from the store is untrusted outright. ``extra`` merges additional
    verifiers (e.g. a dev HMAC key in tests) — file entries win on conflict.
    """
    doc = _read_json(Path(path), "trust store")
    keys: dict[str, Verifier] = dict(extra or {})
    entries = doc.get("keys")
    if not isinstance(entries, list) or not entries:
        raise TrustStoreError(f"trust store 沒有任何金鑰：{path}")
    for entry in entries:
        try:
            if entry.get("algorithm") != ED25519_ALGORITHM:
                raise TrustStoreError(
                    f"trust store 內有不支援的演算法：{entry.get('algorithm')!r}"
                    "（dev HMAC 金鑰不進 trust store，用 --trust 傳）")
            keys[entry["key_id"]] = Ed25519Verifier(bytes.fromhex(entry["public_key"]))
        except (KeyError, ValueError) as exc:
            raise TrustStoreError(f"trust store 項目格式不對：{entry!r}（{exc}）") from exc
    return MultiKeyVerifier(keys)


def add_to_trust_store(path: Path | str, entry: dict) -> None:
    """Append ``entry`` to the store at ``path`` (created if missing)."""
    path = Path(path)
    if path.is_file():
        doc = _read_json(path, "trust store")
        entries = list(doc.get("keys", []))
    else:
        entries = []
    if any(e.get("key_id") == entry["key_id"] for e in entries):
        raise TrustStoreError(f"trust store 已有 key_id={entry['key_id']!r}；輪替請用新的 key_id")
    entries.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": TRUST_SCHEMA, "keys": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def sign_napp(napp_path: Path | str, signer) -> SignatureBundle:
    """Attach a detached signature to an existing (unsigned) ``.napp``.

    The canonical digest is recomputed from ``checksums.json`` — we sign what
    is actually in the package, never a caller-provided digest.
    """
    napp_path = Path(napp_path)
    with zipfile.ZipFile(napp_path) as zf:
        names = set(zf.namelist())
        if L.SIGNATURE_JSON in names:
            raise SignatureInvalid(
                f"{napp_path.name} 已有簽章；重簽請重建 package（簽章不可覆蓋）")
        try:
            checksums_doc = json.loads(zf.read(L.CHECKSUMS_JSON).decode("utf-8"))
        except KeyError as exc:
            raise InvalidManifest(f"{napp_path.name} 缺 {L.CHECKSUMS_JSON}") from exc
    digest = L.canonical_digest(checksums_doc.get("files", {}))
    bundle = sign_digest(signer, digest)
    with zipfile.ZipFile(napp_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(L.SIGNATURE_JSON, json.dumps(bundle.to_json(), ensure_ascii=False, indent=2))
    return bundle


def _read_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise TrustStoreError(f"找不到 {label}：{path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise TrustStoreError(f"{label} 不是有效 JSON：{path}（{exc}）") from exc
    if not isinstance(doc, dict):
        raise TrustStoreError(f"{label} 頂層必須是物件：{path}")
    return doc

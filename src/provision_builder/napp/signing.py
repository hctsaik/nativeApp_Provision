"""Detached signatures over a ``.napp`` canonical digest (Slice 4).

The production algorithm (Ed25519) and key distribution are fixed by the P2 ADR
(docs/adr/0001-package-signing.md); this module provides the *shape* and a
stdlib development signer so sign/verify is exercised offline today. Swapping in
Ed25519 later means adding one Signer/Verifier pair — the ``.napp`` format,
``signature.json`` schema and call sites do not change.

WARNING: :class:`DevHmacSigner` is symmetric (HMAC-SHA256). It proves integrity
and that the holder of the shared secret signed it — it is NOT a production
publisher identity. Never ship it to devices as a trust root.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Protocol

from provision_builder.napp.errors import SignatureInvalid

DEV_ALGORITHM = "hmac-sha256-dev"


@dataclass(frozen=True)
class SignatureBundle:
    algorithm: str
    key_id: str
    canonical_digest: str  # hex sha256 the signature commits to
    signature: str         # hex

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "SignatureBundle":
        try:
            return cls(
                algorithm=data["algorithm"],
                key_id=data["key_id"],
                canonical_digest=data["canonical_digest"],
                signature=data["signature"],
            )
        except (KeyError, TypeError) as exc:
            raise SignatureInvalid(f"malformed signature.json: {exc}") from exc


class Signer(Protocol):
    algorithm: str
    key_id: str

    def sign(self, digest_hex: str) -> str: ...


class Verifier(Protocol):
    def verify(self, digest_hex: str, bundle: SignatureBundle) -> None: ...


class DevHmacSigner:
    """Development-only symmetric signer/verifier (see module warning)."""

    algorithm = DEV_ALGORITHM

    def __init__(self, secret: bytes = b"provision-dev-key", key_id: str = "dev"):
        self._secret = secret
        self.key_id = key_id

    def _mac(self, digest_hex: str) -> str:
        return hmac.new(self._secret, digest_hex.encode("ascii"), sha256).hexdigest()

    def sign(self, digest_hex: str) -> str:
        return self._mac(digest_hex)

    def verify(self, digest_hex: str, bundle: SignatureBundle) -> None:
        if bundle.algorithm != self.algorithm:
            raise SignatureInvalid(f"unexpected algorithm: {bundle.algorithm}")
        if bundle.canonical_digest != digest_hex:
            raise SignatureInvalid("signature commits to a different digest")
        if not hmac.compare_digest(bundle.signature, self._mac(digest_hex)):
            raise SignatureInvalid("signature does not verify")


class MultiKeyVerifier:
    """Device trust-store: verify against whichever trusted key signed it.

    Models key rotation (P2 ADR §4): multiple valid keys can coexist during an
    overlap; ``retire`` drops a key so newly-signed-with-it packages stop
    verifying while older ones signed by still-trusted keys keep working.
    A key_id not in the store is rejected outright.
    """

    def __init__(self, verifiers_by_key_id: dict[str, Verifier]):
        self._verifiers = dict(verifiers_by_key_id)

    def retire(self, key_id: str) -> None:
        self._verifiers.pop(key_id, None)

    def trusted_key_ids(self) -> set[str]:
        return set(self._verifiers)

    def verify(self, digest_hex: str, bundle: SignatureBundle) -> None:
        verifier = self._verifiers.get(bundle.key_id)
        if verifier is None:
            raise SignatureInvalid(f"untrusted key_id: {bundle.key_id!r}")
        verifier.verify(digest_hex, bundle)


ED25519_ALGORITHM = "ed25519"


class Ed25519Signer:
    """Production publisher signer (ADR 0001), pure-Python RFC 8032.

    ``seed`` is the 32-byte private seed. Keep it in a secret store — never in
    a repo, a package, or a device trust-store.
    """

    algorithm = ED25519_ALGORITHM

    def __init__(self, seed: bytes, key_id: str):
        from provision_builder.napp import ed25519

        self._seed = seed
        self.key_id = key_id
        self.public_key = ed25519.secret_to_public(seed)

    def sign(self, digest_hex: str) -> str:
        from provision_builder.napp import ed25519

        return ed25519.sign(self._seed, digest_hex.encode("ascii")).hex()

    def verify(self, digest_hex: str, bundle: SignatureBundle) -> None:
        Ed25519Verifier(self.public_key).verify(digest_hex, bundle)


class Ed25519Verifier:
    """Verify against one trusted Ed25519 public key (device side: no secret)."""

    def __init__(self, public_key: bytes):
        self._public = public_key

    def verify(self, digest_hex: str, bundle: SignatureBundle) -> None:
        from provision_builder.napp import ed25519

        if bundle.algorithm != ED25519_ALGORITHM:
            raise SignatureInvalid(f"unexpected algorithm: {bundle.algorithm}")
        if bundle.canonical_digest != digest_hex:
            raise SignatureInvalid("signature commits to a different digest")
        try:
            raw = bytes.fromhex(bundle.signature)
        except ValueError as exc:
            raise SignatureInvalid("signature is not valid hex") from exc
        if not ed25519.verify(self._public, digest_hex.encode("ascii"), raw):
            raise SignatureInvalid("signature does not verify")


def sign_digest(signer: Signer, digest_hex: str) -> SignatureBundle:
    return SignatureBundle(
        algorithm=signer.algorithm,
        key_id=signer.key_id,
        canonical_digest=digest_hex,
        signature=signer.sign(digest_hex),
    )


def verify_bundle(verifier: Verifier, digest_hex: str, bundle: SignatureBundle) -> None:
    verifier.verify(digest_hex, bundle)

"""純 Python Ed25519（RFC 8032）— 正確性由官方測試向量釘住。

向量來源：RFC 8032 §7.1（TEST 1–3）。任何實作改動讓這三組不過 = 實作錯，
不是向量錯。
"""

from __future__ import annotations

import pytest

from provision_builder.napp import ed25519
from provision_builder.napp.signing import (
    DevHmacSigner,
    Ed25519Signer,
    Ed25519Verifier,
    SignatureInvalid,
    sign_digest,
)

# (secret, public, message, signature) — RFC 8032 §7.1
RFC8032_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bac"
        "c61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e"
        "458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290"
        "ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize("secret,public,message,signature", RFC8032_VECTORS)
def test_rfc8032_vectors(secret: str, public: str, message: str, signature: str) -> None:
    seed = bytes.fromhex(secret)
    msg = bytes.fromhex(message)
    assert ed25519.secret_to_public(seed).hex() == public
    assert ed25519.sign(seed, msg).hex() == signature
    assert ed25519.verify(bytes.fromhex(public), msg, bytes.fromhex(signature))


def test_verify_rejects_wrong_message_and_bitflips() -> None:
    seed = bytes.fromhex(RFC8032_VECTORS[0][0])
    public = ed25519.secret_to_public(seed)
    sig = ed25519.sign(seed, b"right message")
    assert ed25519.verify(public, b"right message", sig)
    assert not ed25519.verify(public, b"wrong message", sig)
    for index in (0, 31, 32, 63):  # R 前後、s 前後各翻一個 bit
        broken = bytearray(sig)
        broken[index] ^= 0x01
        assert not ed25519.verify(public, b"right message", bytes(broken))


def test_verify_rejects_non_canonical_s() -> None:
    """s >= L 的簽章（可鍛性）必須被 strict 驗證拒絕。"""
    seed = bytes.fromhex(RFC8032_VECTORS[0][0])
    public = ed25519.secret_to_public(seed)
    sig = ed25519.sign(seed, b"m")
    L = 2**252 + 27742317777372353535851937790883648493
    s = int.from_bytes(sig[32:], "little")
    malleated = sig[:32] + int.to_bytes(s + L, 32, "little")
    assert not ed25519.verify(public, b"m", malleated)


def test_bad_lengths_and_garbage_points() -> None:
    seed = bytes.fromhex(RFC8032_VECTORS[0][0])
    public = ed25519.secret_to_public(seed)
    sig = ed25519.sign(seed, b"m")
    assert not ed25519.verify(public[:31], b"m", sig)
    assert not ed25519.verify(public, b"m", sig[:63])
    assert not ed25519.verify(b"\xff" * 32, b"m", sig)
    with pytest.raises(ValueError):
        ed25519.sign(b"short seed", b"m")


# ---------------------------------------------------------------------------
# Signer/Verifier 介面（signature.json 語意）
# ---------------------------------------------------------------------------

def test_signer_verifier_roundtrip() -> None:
    signer = Ed25519Signer(bytes.fromhex(RFC8032_VECTORS[0][0]), "team-a")
    digest = "ab" * 32
    bundle = sign_digest(signer, digest)
    assert bundle.algorithm == "ed25519" and bundle.key_id == "team-a"
    Ed25519Verifier(signer.public_key).verify(digest, bundle)  # 不丟例外 = 通過

    with pytest.raises(SignatureInvalid, match="different digest"):
        Ed25519Verifier(signer.public_key).verify("cd" * 32, bundle)

    other = Ed25519Signer(bytes.fromhex(RFC8032_VECTORS[1][0]), "team-b")
    with pytest.raises(SignatureInvalid, match="does not verify"):
        Ed25519Verifier(other.public_key).verify(digest, bundle)


def test_verifier_rejects_dev_hmac_bundle() -> None:
    """演算法不符（dev HMAC 混進 production 驗證路徑）必須被拒。"""
    digest = "ab" * 32
    bundle = sign_digest(DevHmacSigner(), digest)
    signer = Ed25519Signer(bytes.fromhex(RFC8032_VECTORS[0][0]), "team-a")
    with pytest.raises(SignatureInvalid, match="algorithm"):
        Ed25519Verifier(signer.public_key).verify(digest, bundle)

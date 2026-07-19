"""Pure-Python Ed25519 (RFC 8032) — production signing without native code.

ADR 0001 picked Ed25519 but deferred it assuming it needs the ``cryptography``
package (native code — exactly what WDAC blocks on the build box). It does not:
RFC 8032 §6 publishes a complete reference implementation needing only
``hashlib.sha512`` and big-int arithmetic, both stdlib. What we sign is a single
64-hex-char canonical digest per package, so the ~ms cost of pure Python is
irrelevant.

Security notes (deliberate, reviewed trade-offs):

- **Not constant-time.** Verification handles only public data, so timing leaks
  nothing. Signing runs on the build machine with the operator's own key — the
  threat model there is key theft (filesystem), not a local timing oracle. Do
  NOT lift this module into a network service that signs attacker-chosen data.
- **Strict verification**: rejects ``s >= L`` (malleability) and any
  non-decodable point, per RFC 8032.
- Correctness is pinned by the RFC 8032 §7.1 test vectors in
  ``tests/test_ed25519.py``.
"""

from __future__ import annotations

import hashlib

__all__ = ["secret_to_public", "sign", "verify", "SEED_BYTES", "SIGNATURE_BYTES"]

SEED_BYTES = 32
SIGNATURE_BYTES = 64

_p = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493

_d = -121665 * pow(121666, _p - 2, _p) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)

Point = tuple[int, int, int, int]  # extended homogeneous (X, Y, Z, T)

_IDENT: Point = (0, 1, 1, 0)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _point_add(P: Point, Q: Point) -> Point:
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _point_mul(s: int, P: Point) -> Point:
    Q = _IDENT
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _point_equal(P: Point, Q: Point) -> bool:
    if (P[0] * Q[2] - Q[0] * P[2]) % _p:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % _p:
        return False
    return True


def _recover_x(y: int, sign_bit: int) -> int | None:
    if y >= _p:
        return None
    x2 = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p) % _p
    if x2 == 0:
        return None if sign_bit else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p:
        x = x * _sqrt_m1 % _p
    if (x * x - x2) % _p:
        return None
    if (x & 1) != sign_bit:
        x = _p - x
    return x


_g_y = 4 * pow(5, _p - 2, _p) % _p
_g_x = _recover_x(_g_y, 0)
assert _g_x is not None
_G: Point = (_g_x, _g_y, 1, _g_x * _g_y % _p)


def _compress(P: Point) -> bytes:
    zinv = pow(P[2], _p - 2, _p)
    x = P[0] * zinv % _p
    y = P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data: bytes) -> Point | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign_bit = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign_bit)
    if x is None:
        return None
    return (x, y, 1, x * y % _p)


def _secret_expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != SEED_BYTES:
        raise ValueError(f"Ed25519 seed must be {SEED_BYTES} bytes, got {len(seed)}")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def secret_to_public(seed: bytes) -> bytes:
    a, _ = _secret_expand(seed)
    return _compress(_point_mul(a, _G))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _secret_expand(seed)
    public = _compress(_point_mul(a, _G))
    r = int.from_bytes(_sha512(prefix + message), "little") % _L
    R = _compress(_point_mul(r, _G))
    h = int.from_bytes(_sha512(R + public + message), "little") % _L
    s = (r + h * a) % _L
    return R + int.to_bytes(s, 32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != SIGNATURE_BYTES:
        return False
    A = _decompress(public)
    if A is None:
        return False
    R_bytes = signature[:32]
    R = _decompress(R_bytes)
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:  # strict: non-canonical s = malleable signature, reject
        return False
    h = int.from_bytes(_sha512(R_bytes + public + message), "little") % _L
    return _point_equal(_point_mul(s, _G), _point_add(R, _point_mul(h, A)))

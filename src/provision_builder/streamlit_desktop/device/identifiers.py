"""Identifier validation — every name that ever becomes part of a path.

All paths in this system are built as <trusted root> / <validated identifier>;
manifests never supply raw paths. So this module is the security boundary:
if it accepts a value, joining it under a root cannot escape that root.
"""

from __future__ import annotations

import re

# Starts alphanumeric (rejects "..", ".hidden", "-flag"), then a conservative
# set. Length-capped: identifiers appear in paths on Windows (MAX_PATH).
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class IdentifierError(ValueError):
    pass


def validate_identifier(value: object, what: str) -> str:
    if not isinstance(value, str) or not _SAFE.match(value):
        raise IdentifierError(f"{what} is not a safe identifier: {value!r}")
    # Windows silently strips trailing dots/spaces: "v1." opens "v1".
    if value != value.rstrip(". "):
        raise IdentifierError(f"{what} must not end with dot or space: {value!r}")
    return value


def validate_optional(value: object, what: str) -> str | None:
    return None if value is None else validate_identifier(value, what)


def is_safe_relpath(value: str) -> bool:
    """A manifest-declared file path: relative, forward slashes, no escapes.
    Used by integrity checks on files.json entries."""
    if not isinstance(value, str) or not value or len(value) > 1000:
        return False
    if "\\" in value or value.startswith("/") or ":" in value:
        return False
    segments = value.split("/")
    return all(seg not in ("", ".", "..") and seg == seg.rstrip(". ") for seg in segments)

"""Stable package-domain errors shared by the CLI, HTTP API and future GUI.

Every failure the package domain can surface has a dedicated class carrying a
machine-readable ``code``. Transports (HTTP, GUI) map a failure by its ``code``
without re-deriving business state — the service layer is the only place that
decides which error to raise, and each storage adapter translates its own
exceptions (e.g. ``sqlite3.IntegrityError``) into the matching domain error.

The code strings are a contract: they appear in the HTTP error body and are
asserted by tests. Do not rename them without a schema bump.
"""

from __future__ import annotations

import re

# Single source of truth for app_id / version / channel identifiers. The same
# expression is mirrored by the JSON Schemas, HTTP request validation and the
# registry migrations (see docs/REGISTRY_LOGICAL_SCHEMA.md) — do not fork it.
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PackageDomainError(Exception):
    """Base class for every stable package-domain failure."""

    code = "package_domain_error"


class InvalidIdentifier(PackageDomainError):
    code = "invalid_identifier"


class DuplicateVersion(PackageDomainError):
    code = "duplicate_version"


class ArtifactAlreadyExists(PackageDomainError):
    code = "artifact_already_exists"


class ArtifactMissing(PackageDomainError):
    code = "artifact_missing"


class ArtifactCorrupted(PackageDomainError):
    code = "artifact_corrupted"


class UnknownApplication(PackageDomainError):
    code = "unknown_application"


class UnknownChannel(PackageDomainError):
    code = "unknown_channel"


class ReleaseNotPublished(PackageDomainError):
    code = "release_not_published"


class ReleaseYanked(PackageDomainError):
    code = "release_yanked"


class HashMismatch(PackageDomainError):
    code = "hash_mismatch"


class RegistryUnavailable(PackageDomainError):
    code = "registry_unavailable"


class ObjectStoreUnavailable(PackageDomainError):
    code = "object_store_unavailable"


def validate_identifier(value: str, label: str) -> str:
    """Return ``value`` unchanged when it is a legal identifier.

    Raised as a domain error (not ``ValueError``) so every transport maps it to
    a stable ``invalid_identifier`` code / HTTP 400.
    """
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise InvalidIdentifier(f"invalid {label}: {value!r}")
    return value

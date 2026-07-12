"""Build/verify-time errors for the ``.napp`` format.

They subclass :class:`~provision_builder.package_errors.PackageDomainError` so
they carry a stable ``code`` and can be surfaced by any transport, but they are
*not* part of the 12 registry-domain codes in 03_DOMAIN_SPEC.md §4 — these are
package-assembly concerns, kept separate on purpose.
"""

from __future__ import annotations

from provision_builder.package_errors import PackageDomainError


class InvalidManifest(PackageDomainError):
    code = "invalid_manifest"


class SignatureInvalid(PackageDomainError):
    code = "signature_invalid"

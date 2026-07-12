"""``.napp`` application package format (Slice 4).

A ``.napp`` is a versioned, immutable, verifiable zip that carries application
*source* plus small metadata and *references* to big dependencies — never the
2 GB wheels themselves (those live content-addressed in the blob store, see
02_ARCHITECTURE.md §3):

    package.json              build provenance + fingerprints + hashes
    application/              app source (per-file sha256 in checksums.json)
    dependency-manifest.json  core.deppack-shaped dependency declaration
    blob-references.json      sha256 references to big-dep blobs (no bodies)
    migrations/               data migrations (optional)
    checksums.json            sha256 of every packaged file
    signature.json            detached signature over the canonical digest

Stdlib-only, so it builds and verifies on the WDAC/offline box. ``app.yaml`` is
parsed where PyYAML exists (the engine, as in scan.py); this module loads the
already-structured manifest from JSON or, when available, YAML.
"""

from provision_builder.napp.builder import NappBuildResult, build_napp
from provision_builder.napp.errors import InvalidManifest, SignatureInvalid
from provision_builder.napp.manifest import AppManifest, load_app_manifest
from provision_builder.napp.reader import NappContents, install_source, read_package_json, verify_napp
from provision_builder.napp.signing import (
    DevHmacSigner,
    MultiKeyVerifier,
    SignatureBundle,
    sign_digest,
    verify_bundle,
)

__all__ = [
    "AppManifest",
    "load_app_manifest",
    "build_napp",
    "NappBuildResult",
    "verify_napp",
    "install_source",
    "read_package_json",
    "NappContents",
    "DevHmacSigner",
    "MultiKeyVerifier",
    "SignatureBundle",
    "sign_digest",
    "verify_bundle",
    "InvalidManifest",
    "SignatureInvalid",
]

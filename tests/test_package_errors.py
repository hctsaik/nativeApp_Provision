from __future__ import annotations

import pytest

from provision_builder.package_errors import (
    ArtifactAlreadyExists,
    ArtifactCorrupted,
    ArtifactMissing,
    DuplicateVersion,
    HashMismatch,
    InvalidIdentifier,
    ObjectStoreUnavailable,
    PackageDomainError,
    RegistryUnavailable,
    ReleaseNotPublished,
    ReleaseYanked,
    UnknownApplication,
    UnknownChannel,
    validate_identifier,
)

# 逐字對照 03_DOMAIN_SPEC.md §4;改字串等於改對外契約，這個表就是防手滑的閘門。
EXPECTED_CODES = {
    InvalidIdentifier: "invalid_identifier",
    DuplicateVersion: "duplicate_version",
    ArtifactAlreadyExists: "artifact_already_exists",
    ArtifactMissing: "artifact_missing",
    ArtifactCorrupted: "artifact_corrupted",
    UnknownApplication: "unknown_application",
    UnknownChannel: "unknown_channel",
    ReleaseNotPublished: "release_not_published",
    ReleaseYanked: "release_yanked",
    HashMismatch: "hash_mismatch",
    RegistryUnavailable: "registry_unavailable",
    ObjectStoreUnavailable: "object_store_unavailable",
}


def test_error_codes_are_stable() -> None:
    for cls, code in EXPECTED_CODES.items():
        assert cls.code == code, f"{cls.__name__}.code drifted"
        assert issubclass(cls, PackageDomainError)


def test_domain_error_codes_are_unique() -> None:
    codes = list(EXPECTED_CODES.values())
    assert len(codes) == len(set(codes))


def test_taxonomy_has_twelve_errors() -> None:
    # 12 類 = 03_DOMAIN_SPEC.md §4 表列的全部；少一類代表漏實作。
    assert len(EXPECTED_CODES) == 12


@pytest.mark.parametrize("good", ["cv-reviewer", "1.0.0", "a", "A0", "x.y_z-1", "0", "x" * 128])
def test_validate_identifier_accepts_legal(good: str) -> None:
    assert validate_identifier(good, "id") == good


@pytest.mark.parametrize(
    "bad",
    ["", "../evil", "a b", "a:b", ".hidden", "-lead", "_lead", "a/b", "a\\b", "a\x00b", "x" * 129],
)
def test_validate_identifier_rejects_illegal(bad: str) -> None:
    with pytest.raises(InvalidIdentifier):
        validate_identifier(bad, "id")


def test_validate_identifier_rejects_non_string() -> None:
    with pytest.raises(InvalidIdentifier):
        validate_identifier(None, "id")  # type: ignore[arg-type]

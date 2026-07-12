"""Load and validate the application declaration (``app.yaml`` / ``app.json``)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from provision_builder.napp.errors import InvalidManifest
from provision_builder.napp.schema import validate

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))


APP_SCHEMA = _load_schema("app.schema.json")
PACKAGE_SCHEMA = _load_schema("package.schema.json")


def _load_structured(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # PyYAML lives in the engine's Python (see scan.py), not the builder.
        except ImportError as exc:
            raise InvalidManifest(
                f"reading {path.name} needs PyYAML; run under the engine Python or provide app.json"
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise InvalidManifest(f"{path.name}: top level must be a mapping")
    return data


@dataclass(frozen=True)
class AppManifest:
    id: str
    version: str
    entrypoint: str = ""
    category: str = ""
    source_root: str = "."
    requires: list[str] = field(default_factory=list)
    big_deps: list[str] = field(default_factory=list)
    healthcheck: dict = field(default_factory=dict)
    data_dirs: list[str] = field(default_factory=list)
    migrations: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "AppManifest":
        validate(data, APP_SCHEMA)
        return cls(
            id=data["id"],
            version=data["version"],
            entrypoint=data.get("entrypoint", ""),
            category=data.get("category", ""),
            source_root=data.get("source_root", "."),
            requires=list(data.get("requires", [])),
            big_deps=list(data.get("big_deps", [])),
            healthcheck=dict(data.get("healthcheck", {})),
            data_dirs=list(data.get("data_dirs", [])),
            migrations=data.get("migrations", ""),
            raw=data,
        )


def load_app_manifest(path: Path | str) -> AppManifest:
    path = Path(path)
    if not path.is_file():
        raise InvalidManifest(f"app manifest not found: {path}")
    return AppManifest.from_dict(_load_structured(path))


def validate_package_manifest(data: dict) -> None:
    validate(data, PACKAGE_SCHEMA)

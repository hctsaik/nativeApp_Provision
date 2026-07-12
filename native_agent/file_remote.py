"""Offline directory channel for USB/air-gapped application delivery."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

from provision_builder.blob_store import FileBlobStore
from provision_builder.package_services import Release


INDEX = "channel.json"


def export_channel(service, blobs: FileBlobStore, destination: Path | str, channel: str = "production") -> Path:
    root = Path(destination)
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    exported: list[dict] = []
    for app_id in service.list_applications():
        release = service.resolve(app_id, channel)
        if release is None:
            continue
        target = root / "artifacts" / f"{app_id}-{release.version}.napp"
        with service.open_artifact(release) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        item = asdict(release)
        item["artifact"] = target.relative_to(root).as_posix()
        exported.append(item)
    blob_target = root / "blobs"
    if blobs.root.is_dir():
        shutil.copytree(blobs.root, blob_target, dirs_exist_ok=True)
    (root / INDEX).write_text(json.dumps({"schema": 1, "channel": channel, "releases": exported}, indent=2), encoding="utf-8")
    return root


class FileChannelRemote:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        doc = json.loads((self.root / INDEX).read_text(encoding="utf-8"))
        self.channel = doc["channel"]
        self._releases = {item["app_id"]: item for item in doc["releases"]}

    def list_applications(self) -> list[str]:
        return sorted(self._releases)

    def resolve(self, app_id: str, channel: str) -> Release | None:
        if channel != self.channel or app_id not in self._releases:
            return None
        item = dict(self._releases[app_id])
        item.pop("artifact")
        return Release(**item)

    def open_artifact(self, release: Release):
        item = self._releases[release.app_id]
        return (self.root / item["artifact"]).open("rb")

    @property
    def blobs(self) -> FileBlobStore:
        return FileBlobStore(self.root / "blobs")

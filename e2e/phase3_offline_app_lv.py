"""Real Phase-3 acceptance: build and install app-lv from the offline wheelhouse."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from native_agent import NativeAgent  # noqa: E402
from provision_builder.blob_store import FileBlobStore  # noqa: E402
from provision_builder.napp import AppManifest, DevHmacSigner, build_napp  # noqa: E402
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry  # noqa: E402


def main() -> int:
    work = ROOT / "e2e" / "phase3-app-lv"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    pack = ROOT / "dist" / "provision" / "packs" / "app-lv"
    manifest = json.loads((pack / "deppack.json").read_text(encoding="utf-8"))
    wheelhouse = work / "wheelhouse"
    shutil.copytree(pack / "wheels", wheelhouse)
    for big_wheel in (ROOT / "dist" / "provision" / "big-deps").glob("*.whl"):
        shutil.copy2(big_wheel, wheelhouse / big_wheel.name)
    source = work / "source"
    source.mkdir()
    (source / "plugin.yaml").write_text("id: app-lv\nrunner: lv\nenabled: true\n", encoding="utf-8")
    (source / "app.py").write_text("VALUE = 'app-lv offline'\n", encoding="utf-8")

    service = PackageService(SQLiteRegistry(work / "registry.db"), FileObjectStore(work / "objects"))
    blobs = FileBlobStore(work / "blobs")
    signer = DevHmacSigner()
    for version in ("1.0.0", "1.0.1"):
        app = AppManifest.from_dict({"id": "app-lv", "version": version, "requires": manifest["requires"],
                                     "healthcheck": {"type": "python-import", "module": "torch"}})
        artifact = work / f"app-lv-{version}.napp"
        build_napp(app, source, artifact, dependency_manifest=manifest,
                   dependency_wheels_dir=wheelhouse,
                   dependency_fingerprint=manifest["requires_fingerprint"], signer=signer)
        service.publish("app-lv", version, artifact)

    service.promote("app-lv", "production", "1.0.0")

    agent = NativeAgent(work / "device", service, blobs, verifier=signer)
    first = agent.update("app-lv", "production")
    if first.state != "UPDATED":
        raise RuntimeError(first)
    # Promote source-only update; dependency fingerprint must reuse the venv.
    service.promote("app-lv", "production", "1.0.1")
    second = agent.update("app-lv", "production")
    if second.state != "UPDATED" or not second.venv_reused:
        raise RuntimeError(second)
    fingerprint = manifest["requires_fingerprint"]
    python = work / "device" / "applications" / "app-lv" / "venvs" / fingerprint / "Scripts" / "python.exe"
    probe = subprocess.run([str(python), "-c", "import torch; print(torch.__version__)"],
                           capture_output=True, text=True)
    report = {"first": first.__dict__, "second": second.__dict__, "torch": probe.stdout.strip(),
              "probe_returncode": probe.returncode}
    (ROOT / "e2e" / "phase3-app-lv-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return probe.returncode


if __name__ == "__main__":
    raise SystemExit(main())

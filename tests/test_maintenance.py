from __future__ import annotations

import io
from pathlib import Path

from provision_builder.maintenance import collect_unreferenced, find_unreferenced
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry


def _service(tmp_path: Path) -> PackageService:
    return PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))


def test_gc_leaves_referenced_objects_untouched(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    pkg = tmp_path / "p.napp"
    pkg.write_bytes(b"real package")
    svc.publish("cv-reviewer", "1.0.0", pkg)
    result = collect_unreferenced(svc.registry, svc.objects)
    assert result.deleted == []
    assert result.referenced == 1
    assert svc.objects.exists("applications/cv-reviewer/1.0.0/cv-reviewer-1.0.0.napp")


def test_gc_collects_orphan_object(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    # Orphan: object present, no registry release (a publish that half-failed).
    orphan_key = "applications/cv-reviewer/9.9.9/cv-reviewer-9.9.9.napp"
    svc.objects.put(orphan_key, io.BytesIO(b"orphan"))
    assert find_unreferenced(svc.registry, svc.objects) == [orphan_key]
    result = collect_unreferenced(svc.registry, svc.objects)
    assert result.deleted == [orphan_key]
    assert not svc.objects.exists(orphan_key)


def test_gc_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    orphan_key = "applications/x/1.0.0/x-1.0.0.napp"
    svc.objects.put(orphan_key, io.BytesIO(b"orphan"))
    result = collect_unreferenced(svc.registry, svc.objects, dry_run=True)
    assert result.deleted == [orphan_key]
    assert svc.objects.exists(orphan_key)  # dry run kept it

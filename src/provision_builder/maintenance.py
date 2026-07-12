"""Safe garbage collection for unreferenced package objects (Slice 3).

A publish that uploads its artifact but then fails to write the registry row
leaves an *unreferenced* object behind (see 03_DOMAIN_SPEC.md §5). GC reclaims
exactly those — an object is removed only when the registry references no
release pointing at it. There is intentionally no way to delete a *referenced*
object: immutability of published releases is never violated by GC.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from provision_builder.package_services import ObjectStore, Registry


@dataclass
class GCResult:
    referenced: int
    scanned: int
    deleted: list[str] = field(default_factory=list)


def find_unreferenced(registry: Registry, objects: ObjectStore) -> list[str]:
    referenced = registry.all_object_keys()
    return [key for key in objects.iter_keys() if key not in referenced]


def collect_unreferenced(registry: Registry, objects: ObjectStore, *, dry_run: bool = False) -> GCResult:
    referenced = registry.all_object_keys()
    scanned = 0
    deleted: list[str] = []
    for key in list(objects.iter_keys()):
        scanned += 1
        if key in referenced:
            continue
        if not dry_run:
            objects.delete(key)  # type: ignore[attr-defined]
        deleted.append(key)
    return GCResult(referenced=len(referenced), scanned=scanned, deleted=deleted)

"""Active leases: which versions/runtimes are being executed right now.

GC must never delete what a running app is using. The lease is a small JSON in
app data; staleness uses PID + process start time (never PID alone — Windows
reuses PIDs, spec §10.3).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

if __package__:
    from . import locks as locks_mod
else:
    import locks as locks_mod


class Lease:
    def __init__(self, path: Path):
        self.path = path

    def release(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass


def create_lease(leases_dir: Path, *, app_id: str, version: str,
                 runtime_fingerprint: str) -> Lease:
    leases_dir = Path(leases_dir)
    leases_dir.mkdir(parents=True, exist_ok=True)
    lease_id = uuid.uuid4().hex
    body = {
        "lease_id": lease_id,
        "app_id": app_id,
        "version": version,
        "runtime_fingerprint": runtime_fingerprint,
        **locks_mod.my_identity(),
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = leases_dir / f"{lease_id}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return Lease(path)


def valid_leases(leases_dir: Path) -> list[dict]:
    """Live leases only; stale ones (owner provably gone) are removed as we go."""
    leases_dir = Path(leases_dir)
    result = []
    if not leases_dir.is_dir():
        return result
    for path in leases_dir.glob("*.json"):
        try:
            meta = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            meta = {}
        if locks_mod.owner_is_stale(meta):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        result.append(meta)
    return result

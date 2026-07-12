"""Build Worker (Slice 5): commit → validated, signed, published ``.napp``.

Orchestrates the existing capabilities (napp builder, PackageService, blob store)
inside an isolated job workspace with a structured JSONL log and a cancellable
subprocess runner. Stdlib-only, so it runs on the build box; the real
checkout / offline-selfcheck / Tauri-E2E steps are injected as hooks so tests
drive the full pipeline offline with local sources and fake checks.
"""

from build_worker.records import BuildRecord, BuildRecordStore
from build_worker.worker import (
    BuildCancelled,
    BuildLog,
    BuildRequest,
    BuildResult,
    BuildWorker,
    run_subprocess,
)

__all__ = [
    "BuildWorker",
    "BuildRequest",
    "BuildResult",
    "BuildLog",
    "BuildCancelled",
    "run_subprocess",
    "BuildRecordStore",
    "BuildRecord",
]

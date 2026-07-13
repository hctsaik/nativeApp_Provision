"""Build a self-contained Streamlit desktop folder: portable Python + the app +
the prebuilt Tauri shell + a launcher, delivered as one copyable folder.

Design and the Phase-0 shell investigation:
docs/SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER_PHASE0_AND_DESIGN.md
"""

from .builder import build, build_manifest, smoke_test
from .store_builder import (
    ExportResult,
    StoreBuildResult,
    VersionInfo,
    build_into_store,
    export_full_tree,
    export_update,
    list_versions,
    newest_version,
    update_needs_runtime,
)
from .discover import (
    Detected,
    default_output,
    find_entrypoint,
    find_runtime,
    find_shell,
    suggest_name,
)
from .models import BuildRequest, BuildResult, app_id_for, slugify
from .validate import declared_packages, validate_request, warnings_for

__all__ = [
    "BuildRequest",
    "BuildResult",
    "Detected",
    "ExportResult",
    "StoreBuildResult",
    "VersionInfo",
    "build_into_store",
    "export_full_tree",
    "export_update",
    "list_versions",
    "newest_version",
    "app_id_for",
    "build",
    "build_manifest",
    "declared_packages",
    "default_output",
    "find_entrypoint",
    "find_runtime",
    "find_shell",
    "slugify",
    "smoke_test",
    "suggest_name",
    "update_needs_runtime",
    "validate_request",
    "warnings_for",
]

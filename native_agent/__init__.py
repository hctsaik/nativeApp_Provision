"""Native_App local update agent (Slice 7).

A Python sidecar — NOT the Rust/Tauri shell, which is frozen under WDAC (see
01_CONSTRAINTS.md W1). It owns the device-side update state machine: resolve the
desired version, download only missing content, verify hash + signature, install
to a version-staging dir, reuse a venv by dependency fingerprint, healthcheck,
atomically activate, promote last-known-good, and roll back / reconcile on
failure. Authoritative device state lives in ``agent/state.db``; a boot-time
``reconcile`` repairs any operation interrupted by a crash or power loss.

Stdlib-only. In the lab the "remote" (Control Plane + MinIO) is substituted by a
local ``PackageService`` + ``FileBlobStore``; the agent code is unchanged.
"""

# This repo is run from source (not pip-installed); provision_builder lives under
# src/. Make it importable so `python -m native_agent` works from the repo root.
import sys as _sys
from pathlib import Path as _Path

_src = _Path(__file__).resolve().parents[1] / "src"
if _src.is_dir() and str(_src) not in _sys.path:
    _sys.path.insert(0, str(_src))

from native_agent.agent import NativeAgent, UpdateOutcome
from native_agent.management import ApplicationManagementService, ApplicationManagementView
from native_agent.management_api import ManagementApi
from native_agent.operations import Forbidden, OperationInProgress, OperationRunner
from native_agent.portal import PortalApp, make_portal_server
from native_agent.state import AgentState
from native_agent.file_remote import FileChannelRemote, export_channel

__all__ = [
    "NativeAgent", "UpdateOutcome", "AgentState",
    "PortalApp", "make_portal_server",
    "ApplicationManagementService", "ApplicationManagementView", "ManagementApi",
    "OperationRunner", "OperationInProgress", "Forbidden",
    "FileChannelRemote", "export_channel",
]

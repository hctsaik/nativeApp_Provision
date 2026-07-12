"""HTTP Control Plane for the CV Reviewer update system (Slice 2).

Transport-only layer over :class:`provision_builder.package_services.PackageService`.
Every route delegates to the shared use case and maps
:class:`~provision_builder.package_errors.PackageDomainError` to a stable HTTP
status by ``code`` — no SQL, object keys or hashing here (architecture #10).

Built on the standard library ``http.server`` so it runs and is fully testable
on the locked-down (WDAC, no-docker) build machine with zero third-party
dependencies. A future FastAPI transport can wrap the same ``HttpApi.handle``.
"""

# Run-from-source bootstrap so `python -m control_plane` finds provision_builder
# (which lives under src/).
import sys as _sys
from pathlib import Path as _Path

_src = _Path(__file__).resolve().parents[1] / "src"
if _src.is_dir() and str(_src) not in _sys.path:
    _sys.path.insert(0, str(_src))

from control_plane.http_api import DEFAULT_ERROR_STATUS, STATUS_BY_CODE, HttpApi, Response

__all__ = ["HttpApi", "Response", "STATUS_BY_CODE", "DEFAULT_ERROR_STATUS"]

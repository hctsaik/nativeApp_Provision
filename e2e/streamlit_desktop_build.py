"""Build the E2E fixture package for real: real portable runtime, real pip.

Separated from the driver so the (slow) build can be reused across driver runs.

    py -3.11 e2e\\streamlit_desktop_build.py [output_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from provision_builder.streamlit_desktop import BuildRequest, build  # noqa: E402

NATIVE_APP = Path(r"C:\code\claude\nativeApp")


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "streamlit-apps"
    fixture = ROOT / "e2e" / "fixtures" / "portable-streamlit-smoke"

    request = BuildRequest(
        project_dir=fixture,
        entrypoint=fixture / "app.py",
        display_name="Portable Streamlit Smoke",
        output_dir=output,
        shell_exe=NATIVE_APP / "apps" / "host-tauri" / "prebuilt" / "cim-light.exe",
        runtime_template=ROOT / ".runtime-cache" / "python311",
    )
    result = build(request, progress=lambda line: print(f"[build] {line}", flush=True))
    print(f"\n[build] {result.summary()}")
    if not result.ok:
        return 1
    print(f"[build] package: {result.package_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

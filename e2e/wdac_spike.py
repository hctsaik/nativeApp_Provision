"""Repeatable WDAC landing-path spike. Run on the actual factory policy image."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cim-wdac-") as raw:
        root = Path(raw)
        staged = root / "staged"
        active = root / "applications" / "app-wdac" / "versions" / "1.0.0"
        staged.mkdir(parents=True)
        probe = staged / "probe.py"
        probe.write_text("print('WDAC_APP_PROBE_OK')\n", encoding="utf-8")
        active.parent.mkdir(parents=True)
        os.replace(staged, active)
        run = subprocess.run([sys.executable, str(active / "probe.py")], capture_output=True, text=True)

        link_ok, link_error = False, ""
        try:
            os.symlink(active, root / "active-link", target_is_directory=True)
            link_ok = (root / "active-link" / "probe.py").is_file()
        except OSError as exc:
            link_error = str(exc)

        report = {
            "policy_note": "Result is authoritative only when run on the real enforced WDAC image.",
            "python": sys.executable,
            "atomic_replace": active.is_dir(),
            "landed_python_execution": run.returncode == 0 and "WDAC_APP_PROBE_OK" in run.stdout,
            "junction_or_symlink": link_ok,
            "junction_error": link_error,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["atomic_replace"] and report["landed_python_execution"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

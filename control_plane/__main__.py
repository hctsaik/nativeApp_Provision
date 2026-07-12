"""``python -m control_plane --root <dir> [--host H] [--port P]``."""

from __future__ import annotations

import argparse
from pathlib import Path

from control_plane.server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CV Reviewer Control Plane (lab)")
    parser.add_argument("--root", type=Path, default=Path(".control-plane"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    print(f"control plane on http://{args.host}:{args.port} (root={args.root.resolve()})")
    serve(args.root.resolve(), args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

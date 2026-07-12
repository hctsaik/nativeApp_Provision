"""Engine shim — the "engine" the prebuilt Tauri shell insists on spawning.

The shell cannot be told to open an arbitrary URL: it always loads its own
baked-in portal, which renders whatever `input_url` the engine reports. So this
shim implements just enough of the engine's control API to say "the app lives
at <url>", and the portal iframes it. Zero Rust rebuild.

Hard rules (see the design doc §3):
  * The shell owns the control port and passes it in: --control-port / --log-dir.
  * It restarts us on crash **with a different control port**, so we keep no
    state: the current Streamlit URL is always asked from the launcher.
  * We own no processes. Stop/Start are forwarded to the launcher over its
    token-protected loopback channel — never a fake success, never a name-scan kill.

stdlib only: it runs on the package's portable Python, which carries Streamlit
but no web framework.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Must stay under the shell's 30s per-call timeout (bridge.rs) so the portal
# sees our error rather than the shell's.
LAUNCHER_TIMEOUT = 25.0

log = logging.getLogger("engine-shim")


class LauncherError(Exception):
    pass


class LauncherClient:
    """Talks to launch.py's control channel. Holds no state of its own."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _call(self, path: str, method: str) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}", method=method,
            headers={"X-CIM-Token": self.token, "Content-Length": "0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=LAUNCHER_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise LauncherError(f"launcher {path} -> {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LauncherError(f"launcher {path} unreachable: {exc}") from exc

    def status(self) -> dict:
        return self._call("/control/status", "GET")

    def start(self) -> dict:
        return self._call("/control/start", "POST")

    def stop(self) -> dict:
        return self._call("/control/stop", "POST")


class ShimConfig:
    def __init__(self, env: dict):
        self.app_id = env.get("CIM_APP_ID") or "app-streamlit"
        self.app_name = env.get("CIM_APP_NAME") or "Streamlit App"
        self.version = env.get("CIM_APP_VERSION") or "1.0.0"
        launcher_url = env.get("CIM_LAUNCHER_URL")
        token = env.get("CIM_LAUNCHER_TOKEN")
        if not launcher_url or not token:
            raise SystemExit(
                "[engine-shim][ERROR] CIM_LAUNCHER_URL / CIM_LAUNCHER_TOKEN are not set. "
                "This shim is only meant to be started by the package launcher (start.bat)."
            )
        self.launcher = LauncherClient(launcher_url, token)


def tool_info(cfg: ShimConfig) -> dict:
    # 'app-' prefix => portal category 'app' => ONE full-height iframe, no
    # input/output split and no cv_framework chrome (engine.py::_derive_category).
    return {"tool_id": cfg.app_id, "name": cfg.app_name, "version": cfg.version, "category": "app"}


def start_response(cfg: ShimConfig, url: str, port: int) -> dict:
    return {
        "tool_id": cfg.app_id,
        "input_url": url,
        "output_url": url,
        "input_port": port,
        "output_port": port,
        "category": "app",
        "sheet_tabs": [],
        "mode": "iframe",
        "ready": True,
        "run_id": f"{cfg.app_id}-{port}",
    }


def make_handler(cfg: ShimConfig, on_shutdown):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s", fmt % args)

        def _reply(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, exc: Exception) -> None:
            # Never tell the portal something worked when it did not.
            log.error("%s", exc)
            self._reply(503, {"detail": str(exc)})

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._reply(200, {"status": "ok"})
            elif path == "/tools":
                self._reply(200, [tool_info(cfg)])
            elif path == "/tools/active/status":
                try:
                    status = cfg.launcher.status()
                except LauncherError as exc:
                    self._fail(exc)
                    return
                if not status.get("running"):
                    self._reply(200, {"active": False})
                    return
                url, port = status["url"], status["port"]
                self._reply(200, {
                    "active": True,
                    "tool_id": cfg.app_id,
                    "category": "app",
                    "input_alive": True,
                    "output_alive": True,
                    "input_url": url,
                    "output_url": url,
                    "input_port": port,
                    "output_port": port,
                    "result_mtime": -1,
                    "run_id": f"{cfg.app_id}-{port}",
                })
            elif path == "/runtime":
                self._reply(200, {"mode": "portable", "app_id": cfg.app_id, "tools": []})
            elif path == "/diagnostics":
                self._reply(200, {"engine": "streamlit-desktop-shim", "app_id": cfg.app_id})
            else:
                self._reply(404, {"detail": f"no route: GET {path}"})

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""

            if path.startswith("/tools/") and path.endswith("/start"):
                requested = path[len("/tools/"):-len("/start")]
                if requested != cfg.app_id:
                    self._reply(404, {"detail": f"Unknown tool: {requested}"})
                    return
                try:
                    result = cfg.launcher.start()
                except LauncherError as exc:
                    self._fail(exc)
                    return
                self._reply(200, start_response(cfg, result["url"], result["port"]))
            elif path == "/tools/stop":
                try:
                    cfg.launcher.stop()
                except LauncherError as exc:
                    self._fail(exc)
                    return
                self._reply(200, {"ok": True})
            elif path == "/selected-paths":
                try:
                    paths = json.loads(raw.decode("utf-8")).get("paths", []) if raw else []
                except ValueError:
                    paths = []
                self._reply(200, {"paths": paths})
            elif path == "/shutdown":
                self._reply(200, {"ok": True})
                on_shutdown()
            else:
                self._reply(404, {"detail": f"no route: POST {path}"})

    return Handler


def build_server(cfg: ShimConfig, control_port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    holder: dict = {}
    httpd = ThreadingHTTPServer((host, control_port),
                                make_handler(cfg, lambda: _shutdown(holder["httpd"])))
    holder["httpd"] = httpd
    return httpd


def _shutdown(httpd: ThreadingHTTPServer) -> None:
    # Can't join our own request thread — hand the shutdown to another one.
    threading.Thread(target=httpd.shutdown, daemon=True).start()


def setup_logging(log_dir: Path) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[engine-shim][ERROR] cannot create log dir {log_dir}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_dir / "engine-shim.log", encoding="utf-8")],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minimal engine for the CIM Tauri shell")
    parser.add_argument("--control-port", type=int, required=True)
    parser.add_argument("--log-dir", required=True)
    # Tolerate flags a future shell may add; never guess at missing required ones.
    args, unknown = parser.parse_known_args(argv)

    setup_logging(Path(args.log_dir))
    if unknown:
        log.info("ignoring unknown args from shell: %s", unknown)

    cfg = ShimConfig(os.environ)
    # Bind on the port the SHELL chose. It re-picks a fresh one whenever it
    # restarts us, so we must never cache or invent it.
    httpd = build_server(cfg, args.control_port)
    log.info("engine-shim listening on 127.0.0.1:%d for %s", args.control_port, cfg.app_id)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        log.info("engine-shim stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""HTTP wiring for the console + fetchers for the Control Plane API."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from web_console.console import ConsoleApp


def in_process_fetch(api):
    """Fetch by calling an in-process ``HttpApi`` (no sockets) — used in the lab/tests."""

    def fetch(path: str):
        resp = api.handle("GET", path, b"")
        try:
            return resp.status, json.loads(resp.body.decode("utf-8"))
        except ValueError:
            return resp.status, None

    return fetch


def in_process_post(api):
    """POST into an in-process ``HttpApi`` with a JSON body."""

    def post(path: str, payload: dict):
        resp = api.handle("POST", path, json.dumps(payload).encode("utf-8"))
        try:
            return resp.status, json.loads(resp.body.decode("utf-8"))
        except ValueError:
            return resp.status, None

    return post


def http_fetch(base_url: str):
    """Fetch against a real Control Plane over HTTP."""
    import urllib.request

    def fetch(path: str):
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except ValueError:
                return exc.code, None

    return fetch


def make_server(console: ConsoleApp, host: str = "127.0.0.1", port: int = 8090) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _write(self, rendered) -> None:
            body = rendered.html.encode("utf-8")
            self.send_response(rendered.status)
            if rendered.location is not None:
                self.send_header("Location", rendered.location)
            self.send_header("Content-Type", rendered.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._write(console.handle(urlsplit(self.path).path))

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            self._write(console.handle_post(urlsplit(self.path).path, form))

        def log_message(self, *_args) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)

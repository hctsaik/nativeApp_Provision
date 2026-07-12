"""Live ``http.server`` wiring around :class:`control_plane.http_api.HttpApi`."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from control_plane.http_api import HttpApi


def make_server(api: HttpApi, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            response = api.handle(method, urlsplit(self.path).path, body)
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, *_args) -> None:  # silence default stderr logging
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve(root: Path | str, host: str = "127.0.0.1", port: int = 8080) -> None:
    from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

    root = Path(root)
    service = PackageService(SQLiteRegistry(root / "registry.db"), FileObjectStore(root / "objects"))
    server = make_server(HttpApi(service, root / "staging"), host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()

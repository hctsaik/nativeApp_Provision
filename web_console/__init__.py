"""Server-rendered Web Console (Slice 6).

Reads through the Control Plane HTTP API and renders plain HTML — no npm / Vite /
esbuild, which are unverified under WDAC (01_CONSTRAINTS.md W2). The console never
touches the DB or MinIO directly; it holds no credentials and only calls the API
(architecture responsibility split). First pages: Applications, Releases,
Channels.
"""

from web_console.console import ConsoleApp, Rendered

__all__ = ["ConsoleApp", "Rendered"]

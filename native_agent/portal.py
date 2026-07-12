"""Native_App device Portal — the user-facing GUI on the device (Slice 7).

Mirrors the ``python -m native_agent`` CLI as clickable buttons: check for an
update, update now, roll back, reconcile after a crash, and GC old versions. It
shows what a normal user sees — current version, whether an update is available,
and a plain-language result — not the machinery. Stdlib http.server, server-
rendered HTML, no third-party packages.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from native_agent.agent import NativeAgent
from native_agent.management import ApplicationManagementService
from native_agent.operations import OperationRunner
from provision_builder.package_errors import PackageDomainError

_STYLE = """
:root{color-scheme:light dark}
body{font-family:system-ui,"Segoe UI","Noto Sans TC",sans-serif;margin:0;background:#0d1b22;color:#e2edf0;
  min-height:100vh;display:flex;align-items:flex-start;justify-content:center;padding:6vh 16px}
.card{width:100%;max-width:520px;background:#12242d;border:1px solid #22343d;border-radius:16px;overflow:hidden;
  box-shadow:0 12px 40px #0006}
.hd{padding:20px 24px;border-bottom:1px solid #22343d}
.hd .app{font-family:ui-monospace,Consolas,monospace;color:#46d0dd;font-size:13px;letter-spacing:.04em}
.hd h1{margin:.2em 0 0;font-size:22px;font-weight:750}
.bd{padding:22px 24px}
.rows{display:flex;flex-direction:column;gap:2px;margin:0 0 18px}
.row{display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid #1b2c34}
.row .k{color:#8ea3ac;font-size:14px}
.row .v{font-family:ui-monospace,Consolas,monospace;font-size:14px}
.banner{padding:12px 14px;border-radius:11px;font-size:14px;margin:0 0 16px;border:1px solid}
.banner.up{background:#46d0dd18;border-color:#46d0dd55;color:#8fe6ee}
.banner.ok{background:#43c88318;border-color:#43c88355;color:#8fe3b4}
.banner.info{background:#ffffff0c;border-color:#ffffff22;color:#b9cbd2}
.actions{display:flex;flex-wrap:wrap;gap:10px}
form{margin:0}
button{font-size:14px;padding:10px 16px;border-radius:10px;border:1px solid #2b3f49;background:#1a2f38;color:#e2edf0;cursor:pointer}
button.primary{background:#46d0dd;border-color:#46d0dd;color:#062028;font-weight:650}
button:hover{filter:brightness(1.08)}
.foot{padding:14px 24px;border-top:1px solid #22343d;color:#7f949d;font-size:12px}
"""


@dataclass
class Rendered:
    status: int
    html: str
    content_type: str = "text/html; charset=utf-8"
    location: str | None = None


def _esc(v: object) -> str:
    return html.escape(str(v))


_MESSAGES = {
    "UPDATED": ("ok", "已更新到最新版本。"),
    "START_ACTIVE": ("info", "已是最新版本,無需更新。"),
    "START_CACHED": ("info", "目前無法連線更新來源,維持現有版本。"),
    "SKIPPED_YANKED": ("info", "遠端指向的版本已被撤回,維持現有版本。"),
    "SKIPPED_FAILED": ("info", "此版本先前安裝失敗,已略過(可用『強制』重試)。"),
    "FAILED": ("info", "更新失敗,已保留原本可用的版本。"),
    "ROLLED_BACK": ("ok", "已退回上一個正常版本。"),
}


class PortalApp:
    def __init__(self, agent: NativeAgent, app_id: str, channel: str = "production"):
        self.agent = agent
        self.app_id = app_id
        self.channel = channel
        self._last: str | None = None
        # Portal is a diagnostic client of the same service the Management Center
        # uses — mutations go through the async runner, not straight to the agent.
        self.runner = OperationRunner(agent)
        self.service = ApplicationManagementService(agent, self.runner, channel)

    def _state(self):
        active = self.agent.state.active_version(self.app_id)
        lkg = self.agent.state.last_known_good(self.app_id)
        try:
            release = self.agent.check(self.app_id, self.channel)
            desired = release.version if release and release.status != "yanked" else None
        except PackageDomainError:
            desired = None
        return active, lkg, desired

    def handle(self, path: str) -> Rendered:
        if urlsplit(path).path != "/":
            return Rendered(404, _page("找不到頁面", "<div class='bd'>404</div>"))
        active, lkg, desired = self._state()
        update_available = desired is not None and desired != active

        if self._last:
            kind, text = _MESSAGES.get(self._last, ("info", self._last))
            banner = f"<div class='banner {kind}'>{_esc(text)}</div>"
        elif update_available:
            banner = f"<div class='banner up'>有可用更新:<b>{_esc(desired)}</b> —— 按「立即更新」安裝。</div>"
        else:
            banner = "<div class='banner ok'>已是最新版本。</div>"

        force = "&nbsp;<label style='font-size:13px;color:#8ea3ac'><input type='checkbox' name='force' value='1'> 強制</label>"
        body = f"""
          <div class='bd'>
            {banner}
            <div class='rows'>
              <div class='row'><span class='k'>目前版本</span><span class='v'>{_esc(active or '未安裝')}</span></div>
              <div class='row'><span class='k'>上一個正常版本 (LKG)</span><span class='v'>{_esc(lkg or '—')}</span></div>
              <div class='row'><span class='k'>可更新到</span><span class='v'>{_esc(desired or '—')}</span></div>
            </div>
            <div class='actions'>
              <form method='post' action='/update'>{force}<button class='primary' type='submit'>立即更新</button></form>
              <form method='post' action='/rollback'><button type='submit'>退回上一版</button></form>
              <form method='post' action='/reconcile'><button type='submit'>修復 (斷電後)</button></form>
              <form method='post' action='/gc'><button type='submit'>清理舊版本</button></form>
              <form method='get' action='/'><button type='submit'>重新整理</button></form>
            </div>
          </div>
          <div class='foot'>裝置 Agent · channel = {_esc(self.channel)} · 純本機執行,失敗自動保留可用版本</div>
        """
        return Rendered(200, _page(f"{self.app_id} 更新", body))

    def handle_post(self, path: str, form: dict) -> Rendered:
        action = urlsplit(path).path
        try:
            if action == "/update":
                early, op_id = self.service.update(self.app_id, force=bool(form.get("force")))
                if op_id is not None:            # a real update runs in the background
                    self.runner.wait(op_id)      # Portal is diagnostic → wait synchronously
                    outcome = self.runner.result(op_id)
                    self._last = outcome.state if outcome else "FAILED"
                else:
                    self._last = early.state
            elif action == "/rollback":
                self._last = self.service.rollback(self.app_id).state
            elif action == "/reconcile":
                self.service.reconcile(self.app_id)
                self._last = "START_ACTIVE"
            elif action == "/gc":
                self.service.gc(self.app_id)
                self._last = "START_ACTIVE"
            else:
                return Rendered(404, _page("找不到頁面", "<div class='bd'>404</div>"))
        except PackageDomainError as exc:
            self._last = f"錯誤:{exc}"
        return Rendered(303, "", location="/")


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(title)}</title><style>{_STYLE}</style></head><body>"
            f"<div class='card'><div class='hd'><div class='app'>native_app · device portal</div>"
            f"<h1>{_esc(title)}</h1></div>{body}</div></body></html>")


def make_portal_server(portal: PortalApp, host: str = "127.0.0.1", port: int = 8091,
                       management_api=None) -> ThreadingHTTPServer:
    """Serve the diagnostic Portal HTML, and — when given — the device-local
    ``/management`` JSON API on the same port (what the Native App iframes)."""

    def _is_mgmt(path: str) -> bool:
        return management_api is not None and urlsplit(path).path.startswith("/management")

    def _applications_page() -> Rendered:
        cards = []
        for view in management_api.service.list_views():
            primary = "update" if view.installed else "install"
            disabled = "" if (view.can_update or view.can_install) else " disabled"
            cards.append(
                "<section class='bd'><h2>" + _esc(view.display_name) + "</h2>"
                "<p><code>" + _esc(view.app_id) + "</code> · " + _esc(view.category) + "</p>"
                "<p>Active: <b>" + _esc(view.active_version or "not installed") + "</b> · "
                "Latest: <b>" + _esc(view.latest_version or "unavailable") + "</b> · "
                "State: <b>" + _esc(view.update_state) + "</b></p>"
                f"<form method='post' action='/applications/{_esc(view.app_id)}/{primary}'>"
                f"<button{disabled}>{primary.title()}</button></form></section>"
            )
        return Rendered(200, _page("Applications", "".join(cards) or "<div class='bd'>No applications</div>"))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _write(self, rendered: Rendered) -> None:
            body = rendered.html.encode("utf-8")
            self.send_response(rendered.status)
            if rendered.location is not None:
                self.send_header("Location", rendered.location)
            self.send_header("Content-Type", rendered.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_api(self, resp) -> None:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            self.wfile.write(resp.body)

        def _raw_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def do_GET(self):  # noqa: N802
            if management_api is not None and urlsplit(self.path).path == "/applications":
                self._write(_applications_page())
            elif _is_mgmt(self.path):
                self._write_api(management_api.handle("GET", self.path, b"",
                                                      {"X-Role": self.headers.get("X-Role", "user")}))
            else:
                self._write(portal.handle(self.path))

        def do_POST(self):  # noqa: N802
            raw = self._raw_body()
            app_match = re.fullmatch(r"/applications/([^/]+)/(install|update|rollback)", urlsplit(self.path).path)
            if app_match and management_api is not None:
                app_id, action = app_match.groups()
                management_api.handle("POST", f"/management/applications/{app_id}/{action}", b"", {"X-Role": "admin"})
                self._write(Rendered(303, "", location="/applications"))
            elif _is_mgmt(self.path):
                self._write_api(management_api.handle("POST", self.path, raw,
                                                      {"X-Role": self.headers.get("X-Role", "user")}))
            else:
                form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
                self._write(portal.handle_post(self.path, form))

        def log_message(self, *_a):
            pass

    return ThreadingHTTPServer((host, port), Handler)

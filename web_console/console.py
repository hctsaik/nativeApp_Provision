"""Console rendering: fetch JSON from the API, emit HTML; POST actions call back
into the API. The console holds no DB/MinIO credentials — it only speaks HTTP to
the Control Plane."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Callable

# fetch(path) -> (status, parsed_json); post(path, dict) -> (status, parsed_json).
Fetch = Callable[[str], "tuple[int, object]"]
Post = Callable[[str, dict], "tuple[int, object]"]

CHANNELS = ("dev", "staging", "production")

_STYLE = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:2rem;color:#1a1a1a}
h1,h2{font-weight:600}a{color:#0b5cad;text-decoration:none}a:hover{text-decoration:underline}
table{border-collapse:collapse;margin:.5rem 0 1.5rem}th,td{border:1px solid #d0d0d0;padding:.35rem .7rem;text-align:left}
th{background:#f2f4f7}.yanked{background:#fde8e8;color:#a12}.crumb{color:#777;font-size:.9rem;margin-bottom:1rem}
form.inline{display:inline;margin:0}button{cursor:pointer}fieldset{border:1px solid #ddd;margin:1rem 0;padding:.6rem 1rem}
input,select{padding:.2rem .3rem;margin:.1rem}legend{font-weight:600;padding:0 .3rem}
.hint{font-size:12px;color:#667;margin-top:.35rem}
"""


@dataclass
class Rendered:
    status: int
    html: str
    content_type: str = "text/html; charset=utf-8"
    location: str | None = None  # set for 3xx redirects


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def _esc(value: object) -> str:
    return html.escape(str(value))


class ConsoleApp:
    def __init__(self, fetch: Fetch, post: Post | None = None):
        self._fetch = fetch
        self._post = post
        self._routes = [
            (re.compile(r"^/$"), self._index),
            (re.compile(r"^/applications/(?P<app_id>[^/]+)$"), self._application),
        ]
        self._post_routes = [
            (re.compile(r"^/applications/(?P<app_id>[^/]+)/build$"), self._do_build),
            (re.compile(r"^/applications/(?P<app_id>[^/]+)/promote$"), self._do_promote),
            (re.compile(r"^/applications/(?P<app_id>[^/]+)/yank$"), self._do_yank),
            (re.compile(r"^/applications/(?P<app_id>[^/]+)/rollout$"), self._do_rollout),
            (re.compile(r"^/applications/(?P<app_id>[^/]+)/rollout/(?P<verb>advance|approve|pause|resume)$"), self._do_rollout_verb),
            (re.compile(r"^/applications/(?P<app_id>[^/]+)/device$"), self._do_device),
        ]

    # ── GET ─────────────────────────────────────────────────────────────────

    def handle(self, path: str) -> Rendered:
        for pattern, view in self._routes:
            match = pattern.match(path)
            if match:
                return view(**match.groupdict())
        return Rendered(404, _page("Not found", "<h1>404</h1><p>No such page.</p>"))

    def handle_post(self, path: str, form: dict) -> Rendered:
        if self._post is None:
            return Rendered(405, _page("Read only", "<p>This console is read-only.</p>"))
        for pattern, action in self._post_routes:
            match = pattern.match(path)
            if match:
                return action(form=form, **match.groupdict())
        return Rendered(404, _page("Not found", "<h1>404</h1>"))

    def _index(self) -> Rendered:
        status, payload = self._fetch("/api/v1/applications")
        if status != 200 or not isinstance(payload, dict):
            return Rendered(502, _page("Console", "<h1>Applications</h1><p>Control Plane unavailable.</p>"))
        apps = payload.get("applications", [])
        rows = "".join(
            f"<tr><td><a href='/applications/{_esc(a)}'>{_esc(a)}</a></td></tr>" for a in apps
        ) or "<tr><td><em>no applications</em></td></tr>"
        body = f"<h1>Applications</h1><table><tr><th>Application</th></tr>{rows}</table>"
        return Rendered(200, _page("Applications", body))

    def _application(self, app_id: str) -> Rendered:
        rel_status, releases = self._fetch(f"/api/v1/applications/{app_id}/releases")
        if rel_status != 200 or not isinstance(releases, list):
            return Rendered(404, _page(app_id, f"<h1>{_esc(app_id)}</h1><p>Unknown application.</p>"))

        sections = [
            f"<div class='crumb'><a href='/'>Applications</a> / {_esc(app_id)}</div><h1>{_esc(app_id)}</h1>",
            self._channels_section(app_id),
            self._releases_section(app_id, releases),
            self._builds_section(app_id),
            self._rollout_section(app_id),
        ]
        if self._post is not None:
            sections.append(self._actions_section(app_id, releases))
        return Rendered(200, _page(app_id, "".join(sections)))

    def _channels_section(self, app_id: str) -> str:
        rows = []
        for channel in CHANNELS:
            status, resolved = self._fetch(f"/api/v1/applications/{app_id}/channels/{channel}")
            version = resolved["version"] if status == 200 and isinstance(resolved, dict) else "—"
            rows.append(f"<tr><td>{_esc(channel)}</td><td>{_esc(version)}</td></tr>")
        return "<h2>Channels</h2><table><tr><th>Channel</th><th>Version</th></tr>" + "".join(rows) + "</table>"

    def _releases_section(self, app_id: str, releases: list) -> str:
        rows = []
        for r in releases:
            cls = " class='yanked'" if r.get("status") == "yanked" else ""
            rows.append(
                f"<tr{cls}><td>{_esc(r['version'])}</td><td>{_esc(r['status'])}</td>"
                f"<td><code>{_esc(r['sha256'][:12])}…</code></td><td>{_esc(r['size_bytes'])}</td></tr>"
            )
        body = "".join(rows) or "<tr><td colspan='4'><em>no releases</em></td></tr>"
        return ("<h2>Releases</h2><table><tr><th>Version</th><th>Status</th><th>SHA-256</th>"
                f"<th>Bytes</th></tr>{body}</table>")

    def _builds_section(self, app_id: str) -> str:
        status, builds = self._fetch(f"/api/v1/applications/{app_id}/builds")
        if status != 200 or not isinstance(builds, list):
            return ""  # builds not enabled on this control plane
        rows = "".join(
            f"<tr><td>{_esc(b['version'])}</td><td>{_esc(b['status'])}</td>"
            f"<td>{_esc((b.get('commit') or '')[:8])}</td></tr>" for b in builds
        ) or "<tr><td colspan='3'><em>no builds</em></td></tr>"
        return "<h2>Builds</h2><table><tr><th>Version</th><th>Status</th><th>Commit</th></tr>" + rows + "</table>"

    def _rollout_section(self, app_id: str) -> str:
        status, r = self._fetch(f"/api/v1/applications/{app_id}/rollout")
        if status != 200 or not isinstance(r, dict):
            return "<h2>Rollout</h2><p><em>none active</em></p>"
        table = ("<h2>Rollout</h2><table><tr><th>Version</th><th>Stage %</th><th>Status</th><th>Approved</th></tr>"
                 f"<tr><td>{_esc(r['version'])}</td><td>{_esc(r['stage_percent'])}</td>"
                 f"<td>{_esc(r['status'])}</td><td>{_esc(r.get('approved'))}</td></tr></table>")
        if self._post is None:
            return table
        rid = _esc(r["rollout_id"])
        base = f"/applications/{_esc(app_id)}/rollout"
        controls = (
            f"<form class='inline' method='post' action='{base}/advance'>"
            f"<input type='hidden' name='rollout_id' value='{rid}'>"
            "<input name='stage_percent' value='50' size='4'><button>Advance</button></form> "
            f"<form class='inline' method='post' action='{base}/approve'><input type='hidden' name='rollout_id' value='{rid}'><button>Approve</button></form> "
            f"<form class='inline' method='post' action='{base}/pause'><input type='hidden' name='rollout_id' value='{rid}'><button>Pause</button></form> "
            f"<form class='inline' method='post' action='{base}/resume'><input type='hidden' name='rollout_id' value='{rid}'><button>Resume</button></form>"
        )
        return table + "<p>" + controls + "</p>"

    def _actions_section(self, app_id: str, releases: list) -> str:
        a = _esc(app_id)
        options = "".join(f"<option>{_esc(r['version'])}</option>" for r in releases)
        chan_opts = "".join(f"<option>{c}</option>" for c in CHANNELS)
        return (
            "<h2>Actions</h2>"
            f"<fieldset><legend>Build &amp; publish</legend><form class='inline' method='post' action='/applications/{a}/build'>"
            "<input name='version' placeholder='2.0.0' size='8'>"
            f"<select name='channel'><option value=''>(don't promote)</option>{chan_opts}</select>"
            "<button type='submit'>Build</button></form>"
            "<div class='hint'>從設定好的來源建置一個新版本(lab:內建範例 app)。</div></fieldset>"
            f"<fieldset><legend>Promote</legend><form class='inline' method='post' action='/applications/{a}/promote'>"
            f"<select name='version'>{options}</select><select name='channel'>{chan_opts}</select>"
            "<button type='submit'>Promote</button></form></fieldset>"
            f"<fieldset><legend>Yank</legend><form class='inline' method='post' action='/applications/{a}/yank'>"
            f"<select name='version'>{options}</select><button type='submit'>Yank</button></form></fieldset>"
            f"<fieldset><legend>Start rollout</legend><form class='inline' method='post' action='/applications/{a}/rollout'>"
            f"<select name='version'>{options}</select><input name='stage_percent' value='10' size='4'>"
            "<button type='submit'>Start</button></form></fieldset>"
            f"<fieldset><legend>Register device</legend><form class='inline' method='post' action='/applications/{a}/device'>"
            "<input name='device_id' placeholder='device-42' size='12'>"
            "<input name='group' value='canary' size='8'><button type='submit'>Register</button></form></fieldset>"
        )

    # ── POST actions (call the API, then redirect back to the app page) ──────

    def _redirect(self, app_id: str) -> Rendered:
        return Rendered(303, "", location=f"/applications/{app_id}")

    def _do_promote(self, *, app_id: str, form: dict) -> Rendered:
        self._post(f"/api/v1/applications/{app_id}/releases/{form['version']}/promote",
                   {"channel": form.get("channel", "production")})
        return self._redirect(app_id)

    def _do_yank(self, *, app_id: str, form: dict) -> Rendered:
        self._post(f"/api/v1/applications/{app_id}/releases/{form['version']}/yank", {})
        return self._redirect(app_id)

    def _do_rollout(self, *, app_id: str, form: dict) -> Rendered:
        self._post(f"/api/v1/applications/{app_id}/rollouts",
                   {"version": form["version"], "stage_percent": int(form.get("stage_percent", 10))})
        return self._redirect(app_id)

    def _do_build(self, *, app_id: str, form: dict) -> Rendered:
        payload = {"version": form["version"]}
        if form.get("channel"):
            payload["channel"] = form["channel"]
        self._post(f"/api/v1/applications/{app_id}/build", payload)
        return self._redirect(app_id)

    def _do_rollout_verb(self, *, app_id: str, verb: str, form: dict) -> Rendered:
        rid = form["rollout_id"]
        payload: dict = {}
        if verb == "advance":
            payload["stage_percent"] = int(form.get("stage_percent", 50))
        self._post(f"/api/v1/rollouts/{rid}/{verb}", payload)
        return self._redirect(app_id)

    def _do_device(self, *, app_id: str, form: dict) -> Rendered:
        self._post("/api/v1/devices", {"device_id": form["device_id"], "group": form.get("group", "default")})
        return self._redirect(app_id)

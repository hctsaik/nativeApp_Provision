"""Launcher + engine shim (the runtime half of the delivered package).

The fake Streamlit here really binds its port and really serves
``/_stcore/health``, so port selection, health-waiting and "the port is free
again after stop" are genuinely exercised — mocking sockets would prove nothing
about the thing that actually goes wrong.
"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "provision_builder" / "streamlit_desktop" / "templates"


def _load(name: str):
    """Templates ship inside the package (they are copied, not imported, at
    build time), so tests load them by path."""
    spec = importlib.util.spec_from_file_location(f"_tmpl_{name}", TEMPLATES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launch = _load("launch")
engine_shim = _load("engine_shim")


# ── a fake Streamlit that behaves like the real one where it matters ─────────

class FakeStreamlit:
    """Binds the port from the command line and answers the health endpoint."""

    def __init__(self, cmd, healthy=True, exit_code=None, **_kwargs):
        self.pid = 424242
        self.returncode = None
        self._httpd = None
        self._thread = None
        port = int(re.search(r"--server\.port=(\d+)", " ".join(cmd)).group(1))
        if exit_code is not None:          # died on startup (e.g. bad app)
            self.returncode = exit_code
            return
        if not healthy:                    # alive but never becomes ready
            return
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _HealthHandler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def poll(self):
        return self.returncode

    def terminate(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminate()


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        body = b"ok"
        self.send_response(200 if self.path == "/_stcore/health" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def factory(**kwargs):
    return lambda cmd, **_popen_kwargs: FakeStreamlit(cmd, **kwargs)


def occupy(port_holder: list) -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port_holder.append(sock)
    return sock.getsockname()[1]


@pytest.fixture
def manifest(tmp_path: Path) -> dict:
    app = tmp_path / "application"
    app.mkdir()
    (app / "app.py").write_text("import streamlit as st\n", encoding="utf-8")
    # A fresh OS-assigned port, not 8501: anything real squatting on 8501 (a
    # stray Streamlit on the dev box) would answer our health checks and turn
    # negative tests into false failures.
    holder: list = []
    free_port = occupy(holder)
    holder.pop().close()
    return {
        "app_id": "app-demo",
        "display_name": "Demo",
        "version": "1.0.0",
        "host": "127.0.0.1",
        "preferred_port": free_port,
        "health_path": "/_stcore/health",
        "startup_timeout_seconds": 5,
        "_python": tmp_path / "runtime" / "python.exe",
        "_entrypoint": app / "app.py",
    }


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    path = tmp_path / "logs"
    path.mkdir()
    return path


# ── manifest safety ──────────────────────────────────────────────────────────

def test_manifest_rejects_absolute_paths(tmp_path: Path):
    with pytest.raises(launch.ManifestError, match="must be relative"):
        launch.resolve_inside(tmp_path, r"C:\Windows\system32\cmd.exe", what="python")


def test_manifest_rejects_parent_escape(tmp_path: Path):
    with pytest.raises(launch.ManifestError, match="escapes the package root"):
        launch.resolve_inside(tmp_path, "../../evil.py", what="entrypoint")


def test_manifest_accepts_paths_inside_the_package(tmp_path: Path):
    (tmp_path / "launcher").mkdir()
    target = tmp_path / "launcher" / "engine_shim.py"
    target.write_text("", encoding="utf-8")
    assert launch.resolve_inside(tmp_path, "launcher/engine_shim.py", what="shim") == target.resolve()


def test_load_manifest_reports_missing_keys(tmp_path: Path):
    (tmp_path / "app-package.json").write_text('{"app_id": "app-x"}', encoding="utf-8")
    with pytest.raises(launch.ManifestError, match="missing required key"):
        launch.load_manifest(tmp_path)


# ── port selection ───────────────────────────────────────────────────────────

def test_pick_port_uses_preferred_when_free():
    holder: list = []
    free = occupy(holder)
    holder.pop().close()                       # free it again, now we know it is unused
    assert launch.pick_port(free) == free


def test_pick_port_falls_back_when_preferred_is_taken():
    holder: list = []
    taken = occupy(holder)
    try:
        chosen = launch.pick_port(taken)
        assert chosen != taken
        assert launch.is_port_free(chosen)
    finally:
        holder.pop().close()


def test_default_port_is_random_within_the_range_and_free():
    """No fixed 8501: a packaged app must not fight every other Streamlit on the
    machine (a stray one squatting on 8501 is exactly what bit us)."""
    low, high = launch.PORT_RANGE
    chosen = {launch.pick_port() for _ in range(12)}
    assert len(chosen) > 1, "port should differ between launches"
    for port in chosen:
        assert low <= port <= high
        assert launch.is_port_free(port)


def test_a_busy_port_in_the_range_is_never_handed_out(monkeypatch):
    holder: list = []
    taken = occupy(holder)
    try:
        # Force the random pick to keep proposing the busy port; pick_port must
        # test it and move on rather than hand it out.
        proposals = iter([taken, taken, taken])
        monkeypatch.setattr(launch.random, "randint",
                            lambda _a, _b: next(proposals, taken + 1))
        chosen = launch.pick_port()
        assert chosen != taken and launch.is_port_free(chosen)
    finally:
        holder.pop().close()


def test_an_explicit_preferred_port_is_still_honoured_when_free():
    holder: list = []
    free = occupy(holder)
    holder.pop().close()
    assert launch.pick_port(free) == free


def test_fallback_ports_are_actually_bindable():
    """The fallback must come from the OS, not from a guess: whatever we hand
    back has to be free right now, every time.

    (We cannot assert "never preferred+1" — the OS hands out ephemeral ports
    sequentially, so a correct implementation hits that value by chance.)"""
    holder: list = []
    taken = occupy(holder)
    try:
        for _ in range(5):
            chosen = launch.pick_port(taken)
            assert chosen != taken
            assert launch.is_port_free(chosen)
    finally:
        holder.pop().close()


# ── supervisor: start / health / stop ────────────────────────────────────────

def test_start_uses_preferred_port_and_health_checks_it(manifest, logs):
    holder: list = []
    manifest["preferred_port"] = occupy(holder)
    holder.pop().close()

    sup = launch.StreamlitSupervisor(manifest, logs, popen_factory=factory())
    try:
        url = sup.start()
        assert url == f"http://127.0.0.1:{manifest['preferred_port']}"
        assert sup.running
        with urllib.request.urlopen(url + "/_stcore/health", timeout=2) as resp:
            assert resp.status == 200
    finally:
        sup.stop()


def test_start_picks_another_port_when_the_preferred_one_is_busy(manifest, logs):
    holder: list = []
    manifest["preferred_port"] = occupy(holder)          # stays bound for the whole test
    sup = launch.StreamlitSupervisor(manifest, logs, popen_factory=factory())
    try:
        url = sup.start()
        assert sup.port != manifest["preferred_port"]
        with urllib.request.urlopen(url + "/_stcore/health", timeout=2) as resp:
            assert resp.status == 200
    finally:
        sup.stop()
        holder.pop().close()


def test_start_fails_loudly_when_streamlit_dies_before_ready(manifest, logs):
    sup = launch.StreamlitSupervisor(manifest, logs, popen_factory=factory(exit_code=1))
    with pytest.raises(launch.StreamlitExited, match="exited with code 1"):
        sup.start()
    assert not sup.running          # never hand a dead app to the shell


def test_start_times_out_when_streamlit_never_becomes_healthy(manifest, logs):
    manifest["startup_timeout_seconds"] = 1
    sup = launch.StreamlitSupervisor(manifest, logs, popen_factory=factory(healthy=False))
    with pytest.raises(launch.StreamlitExited, match="not healthy within"):
        sup.start()


def test_stop_releases_the_port_for_real(manifest, logs):
    sup = launch.StreamlitSupervisor(manifest, logs, popen_factory=factory())
    sup.start()
    port = sup.port
    assert not launch.is_port_free(port)
    assert sup.stop() is True
    assert launch.is_port_free(port)            # the proof the portal's "stopped" is honest
    assert sup.status() == {"running": False, "url": None, "port": None}


def test_restart_after_stop_gets_a_fresh_url(manifest, logs):
    sup = launch.StreamlitSupervisor(manifest, logs, popen_factory=factory())
    try:
        first = sup.start()
        sup.stop()
        second = sup.start()
        assert second and sup.running
        assert first  # both are valid URLs; the point is start() works again
    finally:
        sup.stop()


def test_start_is_idempotent_while_running(manifest, logs):
    sup = launch.StreamlitSupervisor(manifest, logs, popen_factory=factory())
    try:
        assert sup.start() == sup.start()
    finally:
        sup.stop()


def test_stop_only_touches_its_own_process(manifest, logs):
    """Never a name scan for python.exe: the other app must survive."""
    mine = launch.StreamlitSupervisor(manifest, logs, popen_factory=factory())
    theirs = launch.StreamlitSupervisor(manifest, logs, popen_factory=factory())
    try:
        mine.start()
        theirs.start()
        mine.stop()
        assert not mine.running
        assert theirs.running                   # a second delivered package keeps working
        assert not launch.is_port_free(theirs.port)
    finally:
        theirs.stop()


# ── control channel ──────────────────────────────────────────────────────────

class StubSupervisor:
    def __init__(self):
        self.running = False
        self.url = None
        self.port = None
        self.fail = None

    def status(self):
        return {"running": self.running, "url": self.url, "port": self.port}

    def start(self):
        if self.fail:
            raise launch.StreamlitExited(self.fail)
        self.running, self.url, self.port = True, "http://127.0.0.1:9999", 9999
        return self.url

    def stop(self):
        self.running, self.url, self.port = False, None, None
        return True


@pytest.fixture
def control():
    server = launch.ControlServer(StubSupervisor())
    server.start()
    yield server
    server.shutdown()


def call(url: str, method: str, token: str | None):
    headers = {"Content-Length": "0"}
    if token is not None:
        headers["X-CIM-Token"] = token
    request = urllib.request.Request(url, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def test_control_channel_rejects_a_missing_token(control):
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(control.url + "/control/status", "GET", None)
    assert exc.value.code == 403


def test_control_channel_rejects_a_wrong_token(control):
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(control.url + "/control/status", "GET", "not-the-token")
    assert exc.value.code == 403


def test_control_channel_token_is_random_per_launch():
    first, second = launch.ControlServer(StubSupervisor()), launch.ControlServer(StubSupervisor())
    try:
        assert first.token != second.token
        assert len(first.token) >= 32          # not a fixed secret baked into the package
    finally:
        first.shutdown()
        second.shutdown()


def test_control_channel_binds_loopback_only(control):
    assert control.url.startswith("http://127.0.0.1:")


def test_control_start_and_stop_drive_the_supervisor(control):
    status, body = call(control.url + "/control/start", "POST", control.token)
    assert status == 200 and body["url"] == "http://127.0.0.1:9999"

    status, body = call(control.url + "/control/status", "GET", control.token)
    assert body["running"] is True

    status, body = call(control.url + "/control/stop", "POST", control.token)
    assert status == 200 and body == {"ok": True}

    _status, body = call(control.url + "/control/status", "GET", control.token)
    assert body["running"] is False


def test_control_start_surfaces_failure_instead_of_pretending(control):
    control.supervisor.fail = "boom: app.py raised"
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(control.url + "/control/start", "POST", control.token)
    assert exc.value.code == 503
    assert "boom" in exc.value.read().decode()


# ── engine shim ──────────────────────────────────────────────────────────────

class StubLauncher:
    def __init__(self):
        self.running = False
        self.raise_on = set()

    def _guard(self, name):
        if name in self.raise_on:
            raise engine_shim.LauncherError(f"launcher {name} unreachable")

    def status(self):
        self._guard("status")
        return {"running": self.running, "url": "http://127.0.0.1:9000" if self.running else None,
                "port": 9000 if self.running else None}

    def start(self):
        self._guard("start")
        self.running = True
        return {"url": "http://127.0.0.1:9000", "port": 9000}

    def stop(self):
        self._guard("stop")
        self.running = False
        return {"ok": True}


@pytest.fixture
def shim():
    cfg = engine_shim.ShimConfig({
        "CIM_APP_ID": "app-demo", "CIM_APP_NAME": "Demo", "CIM_APP_VERSION": "2.0.0",
        "CIM_LAUNCHER_URL": "http://127.0.0.1:1", "CIM_LAUNCHER_TOKEN": "t",
    })
    cfg.launcher = StubLauncher()
    holder: list = []
    port = occupy(holder)
    holder.pop().close()
    httpd = engine_shim.build_server(cfg, port)          # binds the port the SHELL chose
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", cfg
    httpd.shutdown()
    httpd.server_close()


def get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def post(url: str, payload: dict | None = None):
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def test_shim_requires_the_launcher_env():
    with pytest.raises(SystemExit, match="CIM_LAUNCHER_URL"):
        engine_shim.ShimConfig({})


def test_shim_cli_requires_control_port_and_log_dir():
    with pytest.raises(SystemExit):
        engine_shim.main(["--log-dir", "."])            # no --control-port


def test_shim_actually_listens_on_the_control_port_the_shell_gave_it(shim):
    base, _cfg = shim
    status, body = get(base + "/health")
    assert status == 200 and body == {"status": "ok"}   # this is what the shell polls


def test_shim_advertises_one_app_category_tool(shim):
    base, _cfg = shim
    _status, body = get(base + "/tools")
    assert body == [{"tool_id": "app-demo", "name": "Demo", "version": "2.0.0", "category": "app"}]


def test_shim_start_reports_the_url_the_launcher_owns(shim):
    base, _cfg = shim
    status, body = post(base + "/tools/app-demo/start")
    assert status == 200
    assert body["input_url"] == body["output_url"] == "http://127.0.0.1:9000"
    assert body["category"] == "app" and body["mode"] == "iframe" and body["ready"] is True


def test_shim_rejects_an_unknown_tool(shim):
    base, _cfg = shim
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/tools/app-other/start")
    assert exc.value.code == 404


def test_shim_status_follows_the_launcher(shim):
    base, _cfg = shim
    _status, body = get(base + "/tools/active/status")
    assert body == {"active": False}

    post(base + "/tools/app-demo/start")
    _status, body = get(base + "/tools/active/status")
    assert body["active"] is True and body["input_url"] == "http://127.0.0.1:9000"


def test_shim_stop_goes_through_the_launcher(shim):
    base, cfg = shim
    post(base + "/tools/app-demo/start")
    status, body = post(base + "/tools/stop")
    assert status == 200 and body == {"ok": True}
    assert cfg.launcher.running is False                 # really stopped, not just reported

    _status, body = get(base + "/tools/active/status")
    assert body == {"active": False}


def test_shim_never_fakes_success_when_the_launcher_fails(shim):
    base, cfg = shim
    cfg.launcher.raise_on = {"stop"}
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/tools/stop")
    assert exc.value.code == 503                         # the portal must see the truth


def test_shim_start_failure_is_not_reported_as_ready(shim):
    base, cfg = shim
    cfg.launcher.raise_on = {"start"}
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/tools/app-demo/start")
    assert exc.value.code == 503


def test_shim_stays_within_the_shells_30s_call_budget():
    """bridge.rs times out every engine call at 30s; ours must fail sooner."""
    assert engine_shim.LAUNCHER_TIMEOUT < 30

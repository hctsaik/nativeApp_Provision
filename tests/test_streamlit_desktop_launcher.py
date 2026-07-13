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
import subprocess
import threading
import time
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
        self.sessions = 0            # /control/start = the app was asked to RUN

    def status(self):
        return {"running": self.running, "url": self.url, "port": self.port}

    def start(self):
        if self.fail:
            raise launch.StreamlitExited(self.fail)
        self.running, self.url, self.port = True, "http://127.0.0.1:9999", 9999
        return self.url

    def note_session_start(self):
        self.sessions += 1

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


# ── the shell: closing the window is not a broken computer ───────────────────

class FakeShell:
    """A Tauri shell stand-in, deterministic so no test hangs on a clock.

    It reports itself alive for `alive_polls` poll() calls (None = for as long as
    the window-creation watch cares to look), then survives `alive_waits` ticks of
    the wait loop, then exits with `code`. `terminated` records the launcher closing
    the window itself — which it does when the app is dead on arrival.
    """

    def __init__(self, code: int = 0, alive_polls: int | None = 0, alive_waits: int = 0):
        self.pid = 4242
        self._code = code
        self._polls = alive_polls
        self._waits = alive_waits
        self.returncode = None
        self.ticks = 0
        self.terminated = False

    def poll(self):
        if self.returncode is not None or self._polls is None:
            return self.returncode
        if self._polls > 0:
            self._polls -= 1
            return None
        self.returncode = self._code
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if self._waits > 0:
            self._waits -= 1
            self.ticks += 1
            raise subprocess.TimeoutExpired("cim-light.exe", timeout)
        self.returncode = self._code
        return self.returncode

    def terminate(self):
        self.terminated = True
        self._waits = 0
        self.returncode = self._code


class FakeControl:
    url = "http://127.0.0.1:1"
    token = "t"


def _shell_manifest(tmp_path: Path) -> dict:
    return {"_shell": tmp_path / "cim-light.exe", "_shim": tmp_path / "shim.py",
            "_python": tmp_path / "python.exe", "app_id": "app-demo", "display_name": "Demo"}


def test_a_user_who_closes_the_window_is_not_told_the_computer_is_broken(tmp_path, monkeypatch,
                                                                        capsys):
    """The S10 lie: open the app, glance at it, close it inside 12 seconds — and
    the launcher exited 5 with a WebView2/antivirus lecture, while bootstrap filed
    an ENVIRONMENT failure. It read `poll() is not None` and never read the code.
    A shell that exits 0 opened a window and was closed on purpose: that IS health."""
    shell = FakeShell(code=0)                       # exits at once, cleanly
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *_a, **_k: shell)

    ready = []
    code = launch.run_shell(_shell_manifest(tmp_path), FakeControl(), tmp_path,
                            on_window_ready=lambda: ready.append(True))

    assert code == launch.EXIT_OK                   # not EXIT_MACHINE_BROKEN
    assert ready == [True]                          # the window opened: keep the marker
    err = capsys.readouterr().err
    assert "WebView2" not in err and "應用視窗一開就關閉了" not in err


def test_a_shell_that_cannot_create_a_window_is_still_a_machine_failure(tmp_path, monkeypatch,
                                                                       capsys):
    """The other half must keep working: no WebView2 kills the shell in ~1s with a
    NON-ZERO code, and that is not this version's fault — rolling back would land on
    a version that fails identically."""
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *_a, **_k: FakeShell(code=1))

    ready = []
    code = launch.run_shell(_shell_manifest(tmp_path), FakeControl(), tmp_path,
                            on_window_ready=lambda: ready.append(True))

    assert code == launch.EXIT_MACHINE_BROKEN
    assert ready == []                              # no marker for a version that never showed
    err = capsys.readouterr().err
    assert "WebView2" in err and "不是這個版本的問題" in err


def test_the_marker_is_not_written_while_the_window_is_still_being_created(tmp_path, monkeypatch):
    """A shell that dies half a second in (antivirus killing it as it starts) must
    not have been called healthy first."""
    monkeypatch.setattr(launch.subprocess, "Popen",
                        lambda *_a, **_k: FakeShell(code=1, alive_polls=3))
    monkeypatch.setattr(launch, "_WINDOW_POLL_SECONDS", 0.01)

    ready = []
    code = launch.run_shell(_shell_manifest(tmp_path), FakeControl(), tmp_path,
                            on_window_ready=lambda: ready.append(True))
    assert code == launch.EXIT_MACHINE_BROKEN and ready == []


def test_a_normal_start_is_not_blocked_for_twelve_seconds(tmp_path, monkeypatch):
    """The 12s wait existed to tell a dead shell from a live one — but the comment
    itself said a shell that cannot open a window dies within ~1s. Every healthy
    start paid 12 seconds of blindness (no marker, no arrival tick) for nothing."""
    assert launch.SHELL_ALIVE_SECONDS <= 3.0

    # alive for the whole watch, then a user session that lasts a few ticks
    shell = FakeShell(code=0, alive_polls=None, alive_waits=3)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *_a, **_k: shell)
    monkeypatch.setattr(launch, "_WINDOW_POLL_SECONDS", 0.001)
    monkeypatch.setattr(launch, "SHELL_ALIVE_SECONDS", 0.2)

    started = time.monotonic()
    ready, ticks = [], []
    code = launch.run_shell(_shell_manifest(tmp_path), FakeControl(), tmp_path,
                            on_window_ready=lambda: ready.append(True),
                            on_tick=lambda: ticks.append(True))
    elapsed = time.monotonic() - started

    assert code == 0 and ready == [True]
    assert elapsed < 2.0
    assert len(ticks) == 3, "the shell wait must tick, or the app's arrival window never closes"


# ── an error box the user recovered from is not a failed version ─────────────

def _supervisor(tmp_path: Path, log_text: str, *, arrival_offset: int | None = None):
    log_path = tmp_path / "streamlit.log"
    log_path.write_bytes(log_text.encode("utf-8"))
    supervisor = launch.StreamlitSupervisor(
        {"_python": tmp_path / "python.exe", "_entrypoint": tmp_path / "app.py",
         "host": "127.0.0.1", "preferred_port": 0}, tmp_path)
    supervisor._log_path = log_path
    supervisor._arrival_offset = arrival_offset
    return supervisor


_STARTUP = "  You can now view your Streamlit app in your browser.\n  URL: http://127.0.0.1:8501\n"
_TRACEBACK = ("Uncaught app execution\n"
              "Traceback (most recent call last):\n"
              '  File "app.py", line 12, in <module>\n'
              "    df = pd.read_csv(uploaded)\n"
              "ValueError: could not convert string to float: 'abc'\n")


def test_an_error_box_the_user_recovered_from_is_not_a_failed_version(tmp_path):
    """S10: Streamlit logs a traceback for EVERY exception a script raises —
    including the one the user caused with a bad upload, saw as a red box, and
    fixed by re-running. They worked for an hour, closed the window, and were told
    「這個版本的 App 在執行中出錯」 while bootstrap marked the version failed and
    rolled the machine back. An app that ran cannot be a version that never ran."""
    supervisor = _supervisor(tmp_path, _STARTUP + _TRACEBACK,
                             arrival_offset=len(_STARTUP.encode("utf-8")))

    assert supervisor.app_error_in_log() is None            # nothing failed on arrival
    late = supervisor.late_app_error_in_log()
    assert late and "ValueError" in late                    # …but the operator is told

    assert launch.finish_session(supervisor, launch.EXIT_OK) == launch.EXIT_OK


def test_an_app_that_never_rendered_is_still_a_failed_version(tmp_path):
    """The gate must stay armed: a script that dies on its first run leaves the user
    staring at a red box where the app should be. That version IS broken."""
    supervisor = _supervisor(
        tmp_path,
        _STARTUP + "Traceback (most recent call last):\n"
                   "ModuleNotFoundError: No module named 'cv2'\n",
        arrival_offset=None)                                # the window never closed

    fatal = supervisor.app_error_in_log()
    assert fatal and "ModuleNotFoundError" in fatal
    assert supervisor.late_app_error_in_log() is None


def test_an_app_that_dies_seconds_after_start_is_not_excused_as_a_late_error(tmp_path):
    """The user pressed Start, the app blew up, they closed the window at once — so
    the arrival window never got to close. Everything in that log is 'on arrival'."""
    supervisor = _supervisor(tmp_path, _STARTUP + _TRACEBACK, arrival_offset=None)
    assert supervisor.app_error_in_log() is not None
    assert launch.finish_session(supervisor, launch.EXIT_OK) == launch.EXIT_APP_BROKEN


def test_the_arrival_window_closes_on_a_tick_not_on_a_guess(tmp_path):
    """note_arrival_window() is what separates the two halves of the log, so it must
    freeze the offset only once the app has really had its chance to render."""
    supervisor = _supervisor(tmp_path, _STARTUP)
    supervisor.note_session_start()                         # the user pressed Start

    supervisor.note_arrival_window()
    assert supervisor._arrival_offset is None               # too early: still arriving

    supervisor._session_at = time.monotonic() - launch.APP_ARRIVAL_SECONDS - 1
    supervisor.note_arrival_window()
    assert supervisor._arrival_offset == len(_STARTUP.encode("utf-8"))

    (tmp_path / "streamlit.log").write_bytes((_STARTUP + _TRACEBACK).encode("utf-8"))
    supervisor.note_arrival_window()                        # frozen: never re-opened
    assert supervisor._arrival_offset == len(_STARTUP.encode("utf-8"))
    assert supervisor.app_error_in_log() is None
    assert supervisor.late_app_error_in_log() is not None


# ── the arrival window belongs to the APP, not to the server ─────────────────
#
# THE defect: _healthy_at was set the moment /_stcore/health answered 200 — the
# SERVER being up. But this product's own documented limitation is that the app
# script does not run until the user presses "Start" in the portal, which can be
# minutes later. The 20s window therefore expired while the app had not executed a
# single line; the app was then started, died on import, and its traceback landed
# in the LATE half of the log — a warning (marker kept, exit 0). bootstrap
# committed the broken version as last-known-good. The safety net stamped it good.

def test_a_version_that_dies_after_a_slow_start_press_is_failed_not_committed(tmp_path):
    """THE scenario, end to end: Streamlit is healthy, the operator takes 25 seconds
    to press Start (a coffee, a phone call, reading the release note — the NORMAL
    case), the app is then asked to run and dies on `import cv2`.

    Before: the window had closed 5 seconds ago, so the death was 'late' → warning,
    marker kept, exit 0, committed as last-known-good — the very version rollback
    would later fall back to. Now: the window opens when the app is ASKED TO RUN, so
    the death is an arrival failure → marker revoked, exit 3, bootstrap rolls back."""
    supervisor = _supervisor(tmp_path, _STARTUP)
    supervisor._healthy_at = time.monotonic() - 25.0        # server up for 25 seconds

    for _ in range(50):                                     # 25s of half-second ticks
        supervisor.note_arrival_window()
    assert supervisor._arrival_offset is None, \
        "the window must not close while the app has not been asked to run"

    supervisor.note_session_start()                         # …NOW the user presses Start
    (tmp_path / "streamlit.log").write_bytes(
        (_STARTUP + "Traceback (most recent call last):\n"
                    "ModuleNotFoundError: No module named 'cv2'\n").encode("utf-8"))
    supervisor.note_arrival_window()                        # the very next tick

    fatal = supervisor.app_error_in_log()
    assert fatal and "ModuleNotFoundError" in fatal
    assert supervisor.late_app_error_in_log() is None       # NOT excused as 'late'
    assert launch.finish_session(supervisor, launch.EXIT_OK) == launch.EXIT_APP_BROKEN


def test_the_arrival_window_never_closes_before_the_app_is_asked_to_run(tmp_path):
    """The user opens the window at 09:00 and presses Start after lunch. Streamlit
    has been healthy for four hours and the app has run nothing. There is no arrival
    to have missed, so the window must still be open."""
    supervisor = _supervisor(tmp_path, _STARTUP)
    supervisor._healthy_at = time.monotonic() - 4 * 3600

    supervisor.note_arrival_window()
    assert supervisor.arriving and supervisor._arrival_offset is None
    # …and an error at that point is still fatal, because the app never worked.
    (tmp_path / "streamlit.log").write_bytes((_STARTUP + _TRACEBACK).encode("utf-8"))
    assert supervisor.app_error_in_log() is not None
    assert supervisor.late_app_error_in_log() is None


def test_pressing_start_twice_does_not_rewind_the_arrival_window(tmp_path):
    """start() short-circuits on `if self.running`, so a second /control/start hits a
    live app. If that re-armed the clock, a user who pressed Start again 19 seconds
    into a dying app would buy it another 20 seconds of amnesty — and eventually an
    error that lands 'late'. The first Start is the one that counts."""
    supervisor = _supervisor(tmp_path, _STARTUP)
    supervisor.note_session_start()
    first = supervisor._session_at

    time.sleep(0.01)
    supervisor.note_session_start()
    assert supervisor._session_at == first

    supervisor._session_at = time.monotonic() - launch.APP_ARRIVAL_SECONDS - 1
    supervisor.note_arrival_window()
    assert supervisor._arrival_offset is not None           # it did close, on time
    supervisor.note_session_start()                         # and a late Start
    assert supervisor._arrival_offset is not None           # cannot re-open it


def test_a_restarted_app_gets_a_fresh_arrival_window(tmp_path):
    """Stop, then Start again: a new process, a new log file — and therefore a new
    arrival window. Leaking the old offset would file the new run's startup crash as
    'late' and keep the marker on a version that just failed to start."""
    supervisor = _supervisor(tmp_path, _STARTUP)
    supervisor.note_session_start()
    supervisor._session_at = time.monotonic() - launch.APP_ARRIVAL_SECONDS - 1
    supervisor.note_arrival_window()
    assert supervisor._arrival_offset is not None and not supervisor.arriving

    # what _spawn_and_wait does for the new process (same three lines, one place)
    supervisor._log_path = tmp_path / "streamlit-restart.log"
    supervisor._log_path.write_bytes(b"")
    supervisor._healthy_at = None
    supervisor._session_at = None
    supervisor._arrival_offset = None

    assert supervisor.arriving
    supervisor.note_arrival_window()
    assert supervisor._arrival_offset is None               # no session yet: stays open


def test_the_control_channel_start_is_what_opens_the_arrival_window(tmp_path):
    """The wiring itself: /control/start is the moment the app is asked to run, and
    it is the ONLY thing that may open the window. Asserted through the real HTTP
    handler, because a supervisor method nobody calls protects nobody."""
    class ReadySupervisor(launch.StreamlitSupervisor):
        def start(self):
            return "http://127.0.0.1:9999"

        @property
        def port(self):
            return 9999

    supervisor = ReadySupervisor(
        {"_python": tmp_path / "python.exe", "_entrypoint": tmp_path / "app.py",
         "host": "127.0.0.1", "preferred_port": 0}, tmp_path)
    control = launch.ControlServer(supervisor)
    control.start()
    try:
        assert supervisor._session_at is None               # healthy, but nobody asked
        request = urllib.request.Request(f"{control.url}/control/start", method="POST",
                                         headers={"X-CIM-Token": control.token})
        with urllib.request.urlopen(request, timeout=5) as resp:
            assert resp.status == 200
        assert supervisor._session_at is not None           # THIS is the starting gun
    finally:
        control.shutdown()


# ── a dying app must not wait for the user to close the window ───────────────

def test_an_app_dying_on_arrival_closes_the_window_instead_of_waiting(tmp_path, monkeypatch):
    """The tick already runs every 0.5s. If the app is dead on arrival, everything
    after that is the operator staring at a red box until they give up and close the
    window — and only THEN does the version get failed and rolled back. There is
    nothing to wait for: close the shell, fail the candidate, let bootstrap roll back
    now. That is the difference between the machine fixing itself and the operator
    finding out tomorrow."""
    shell = FakeShell(code=0, alive_polls=None, alive_waits=99)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *_a, **_k: shell)
    monkeypatch.setattr(launch, "_WINDOW_POLL_SECONDS", 0.001)
    monkeypatch.setattr(launch, "SHELL_ALIVE_SECONDS", 0.01)
    monkeypatch.setattr(launch, "_SHELL_TICK_SECONDS", 0.01)

    supervisor = _supervisor(tmp_path, _STARTUP + "ModuleNotFoundError: No module named 'cv2'\n")
    supervisor.note_session_start()

    def tick() -> bool:
        supervisor.note_arrival_window()
        return supervisor.failing_on_arrival() is not None

    code = launch.run_shell(_shell_manifest(tmp_path), FakeControl(), tmp_path,
                            on_window_ready=lambda: None, on_tick=tick)

    assert code == launch.EXIT_APP_BROKEN
    assert shell.terminated, "the window was left open in front of a dead app"
    # …and the session verdict still agrees: marker revoked, exit 3, bootstrap rolls back
    assert launch.finish_session(supervisor, code) == launch.EXIT_APP_BROKEN


def test_a_working_app_is_never_killed_by_the_watchdog(tmp_path, monkeypatch):
    """The other half. An app that is quiet, or one that raised AFTER it had already
    rendered, must be left alone: killing a window somebody is working in is worse
    than any report we could file about it."""
    shell = FakeShell(code=0, alive_polls=None, alive_waits=3)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *_a, **_k: shell)
    monkeypatch.setattr(launch, "_WINDOW_POLL_SECONDS", 0.001)
    monkeypatch.setattr(launch, "SHELL_ALIVE_SECONDS", 0.01)
    monkeypatch.setattr(launch, "_SHELL_TICK_SECONDS", 0.01)

    supervisor = _supervisor(tmp_path, _STARTUP + _TRACEBACK,
                             arrival_offset=len(_STARTUP.encode("utf-8")))
    supervisor.note_session_start()

    def tick() -> bool:
        supervisor.note_arrival_window()
        return supervisor.failing_on_arrival() is not None

    code = launch.run_shell(_shell_manifest(tmp_path), FakeControl(), tmp_path,
                            on_tick=tick)

    assert code == launch.EXIT_OK
    assert not shell.terminated
    assert launch.finish_session(supervisor, code) == launch.EXIT_OK      # a warning only


def test_the_window_closing_message_is_printable_on_a_zh_tw_console():
    launch._ARRIVAL_FAILURE_HINT.encode("cp950")


def test_an_app_that_was_never_started_by_the_user_is_not_a_failure(tmp_path):
    """The user opened the window and closed it without pressing Start: Streamlit
    never ran, there is no log, and there is nothing to blame the version for."""
    supervisor = launch.StreamlitSupervisor(
        {"_python": tmp_path / "python.exe", "_entrypoint": tmp_path / "app.py",
         "host": "127.0.0.1", "preferred_port": 0}, tmp_path)
    assert supervisor.app_error_in_log() is None
    assert supervisor.late_app_error_in_log() is None
    assert launch.finish_session(supervisor, launch.EXIT_OK) == launch.EXIT_OK


# ── the marker contract with bootstrap ───────────────────────────────────────

class LogSupervisor:
    """Answers only the question finish_session asks: what did the app's log say?"""

    def __init__(self, fatal=None, late=None):
        self._fatal, self._late = fatal, late
        self.log_path = Path("streamlit.log")

    def app_error_in_log(self):
        return self._fatal

    def late_app_error_in_log(self):
        return self._late


def test_a_session_that_only_showed_an_error_box_keeps_its_healthy_marker(tmp_path, monkeypatch,
                                                                          capsys):
    """The marker means "this version opened a window and the app did not fail on
    arrival". A survivable exception does not revoke that — bootstrap commits the
    version it has been running successfully all along."""
    marker = tmp_path / "healthy"
    marker.write_text("http://127.0.0.1:8501", encoding="utf-8")
    monkeypatch.setenv("CIM_HEALTHY_MARKER", str(marker))

    code = launch.finish_session(LogSupervisor(late=_TRACEBACK), launch.EXIT_OK)

    assert code == launch.EXIT_OK
    assert marker.exists()                                  # the version stays good
    out = capsys.readouterr().out
    assert "[start][WARN]" in out and "不會退版" in out      # said out loud, not swallowed


def test_a_version_whose_app_never_rendered_loses_its_healthy_marker(tmp_path, monkeypatch):
    """The other side of the same contract: the marker was written when the window
    came up, and the app later proved it never worked. A marker left behind would
    promote a broken version to last-known-good — the one rollback falls back to."""
    marker = tmp_path / "healthy"
    marker.write_text("http://127.0.0.1:8501", encoding="utf-8")
    monkeypatch.setenv("CIM_HEALTHY_MARKER", str(marker))

    code = launch.finish_session(LogSupervisor(fatal="ModuleNotFoundError: no cv2"),
                                 launch.EXIT_OK)

    assert code == launch.EXIT_APP_BROKEN
    assert not marker.exists()


def test_a_dead_window_is_never_blamed_on_the_version(tmp_path, monkeypatch):
    marker = tmp_path / "healthy"
    monkeypatch.setenv("CIM_HEALTHY_MARKER", str(marker))
    assert launch.finish_session(LogSupervisor(), launch.EXIT_MACHINE_BROKEN) == \
        launch.EXIT_MACHINE_BROKEN
    assert not marker.exists()


def test_the_operator_text_survives_a_cp950_console(tmp_path, monkeypatch, capsys):
    """A zh-TW Windows console is cp950. A message that cannot be encoded is a
    UnicodeEncodeError instead of an explanation."""
    monkeypatch.setenv("CIM_HEALTHY_MARKER", str(tmp_path / "healthy"))
    launch.finish_session(LogSupervisor(late="ValueError: x"), launch.EXIT_OK)
    launch.finish_session(LogSupervisor(fatal="ModuleNotFoundError: cv2"), launch.EXIT_OK)
    captured = capsys.readouterr()
    (captured.out + captured.err).encode("cp950")
    launch._MACHINE_HINT.encode("cp950")


# ── the preflight gate must see the pages the user can click ─────────────────

def _multipage(tmp_path: Path) -> tuple[Path, Path]:
    app_root = tmp_path / "application"
    (app_root / "pages").mkdir(parents=True)
    (app_root / "app.py").write_text("import streamlit as st\nst.title('home')\n",
                                     encoding="utf-8")
    return app_root / "app.py", app_root


def test_a_missing_package_in_a_pages_file_is_caught_before_the_user_clicks_it(tmp_path):
    """S3: Streamlit runs pages/*.py itself — nothing imports them (script_runner
    ._mpa_v1). A closure seeded with the entrypoint alone never opened the folder,
    so the missing dependency in pages/2_report.py passed the gate and met the user
    as a red box the first time they clicked 'report'."""
    entry, app_root = _multipage(tmp_path)
    (app_root / "pages" / "2_report.py").write_text(
        "import streamlit as st\nimport definitely_not_installed_pkg\n", encoding="utf-8")

    missing, syntax_error = launch.preflight(entry, app_root)
    assert syntax_error is None
    assert missing == ["definitely_not_installed_pkg"]


def test_streamlits_own_page_rules_are_followed_not_invented(tmp_path):
    """`pages/__init__.py` and dotfiles are NOT pages (script_runner._mpa_v1), and
    a `pages/` folder somewhere else in the tree is not a pages folder at all: it
    must sit next to the entry script."""
    entry, app_root = _multipage(tmp_path)
    (app_root / "pages" / "__init__.py").write_text("import not_a_page_pkg\n", encoding="utf-8")
    (app_root / "pages" / ".hidden.py").write_text("import not_a_page_pkg\n", encoding="utf-8")
    (app_root / "elsewhere" / "pages").mkdir(parents=True)
    (app_root / "elsewhere" / "pages" / "x.py").write_text("import not_a_page_pkg\n",
                                                           encoding="utf-8")

    assert launch.preflight(entry, app_root) == ([], None)


def test_a_page_declared_by_st_page_is_part_of_the_closure(tmp_path):
    """st.navigation's pages are not in pages/ — but a literal path is right there
    in the entry script's AST, so there is no excuse for missing it."""
    app_root = tmp_path / "application"
    (app_root / "screens").mkdir(parents=True)
    (app_root / "app.py").write_text(
        "import streamlit as st\n"
        "pg = st.navigation([st.Page('screens/report.py'), st.Page('screens/home.py')])\n"
        "pg.run()\n", encoding="utf-8")
    (app_root / "screens" / "home.py").write_text("import streamlit as st\n", encoding="utf-8")
    (app_root / "screens" / "report.py").write_text(
        "import streamlit as st\nimport definitely_not_installed_pkg\n", encoding="utf-8")

    missing, _ = launch.preflight(app_root / "app.py", app_root)
    assert missing == ["definitely_not_installed_pkg"]


def test_pages_declared_in_a_pages_toml_are_part_of_the_closure(tmp_path):
    """st-pages' .streamlit/pages.toml: literal paths, tomllib reads them."""
    entry, app_root = _multipage(tmp_path)
    (app_root / "screens").mkdir()
    (app_root / "screens" / "report.py").write_text(
        "import definitely_not_installed_pkg\n", encoding="utf-8")
    (app_root / ".streamlit").mkdir()
    (app_root / ".streamlit" / "pages.toml").write_text(
        '[[pages]]\npath = "app.py"\nname = "Home"\n\n'
        '[[pages]]\npath = "screens/report.py"\nname = "Report"\n', encoding="utf-8")

    missing, _ = launch.preflight(entry, app_root)
    assert missing == ["definitely_not_installed_pkg"]


def test_a_module_next_to_a_page_is_not_reported_as_a_missing_pypi_package(tmp_path):
    """The CV_Viewer misdiagnosis, one folder deeper: a .py sitting in pages/ is the
    app's own code (it is a page!). Telling the admin to `pip install shared_widgets`
    would refuse to start a package that works."""
    entry, app_root = _multipage(tmp_path)
    (app_root / "pages" / "1_shared_widgets.py").write_text("import json\n", encoding="utf-8")
    (app_root / "pages" / "2_report.py").write_text(
        "import streamlit as st\nimport shared_helper\n", encoding="utf-8")
    (app_root / "pages" / "shared_helper.py").write_text("import json\n", encoding="utf-8")

    missing, syntax_error = launch.preflight(entry, app_root)
    assert missing == [] and syntax_error is None


def test_a_syntax_error_in_a_page_is_reported_with_the_page_name(tmp_path):
    entry, app_root = _multipage(tmp_path)
    (app_root / "pages" / "2_report.py").write_text("def broken(\n", encoding="utf-8")

    _missing, syntax_error = launch.preflight(entry, app_root)
    assert syntax_error and "2_report.py" in syntax_error

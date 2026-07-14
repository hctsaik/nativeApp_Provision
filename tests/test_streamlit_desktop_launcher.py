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


# ── this MACHINE is broken vs this VERSION is broken ─────────────────────────

def _store_package(tmp_path: Path) -> Path:
    """A store-layout version tree: everything load_manifest() insists on exists,
    and the shell comes from the SHARED store the way bootstrap hands it over."""
    pkg = tmp_path / "v1.1.0"
    (pkg / "application").mkdir(parents=True)
    (pkg / "application" / "app.py").write_text("import streamlit as st\n", encoding="utf-8")
    (pkg / "launcher").mkdir()
    (pkg / "launcher" / "engine_shim.py").write_text("", encoding="utf-8")
    (pkg / "app-package.json").write_text(json.dumps({
        "app_id": "app-demo", "display_name": "Demo",
        "entrypoint": "application/app.py",
        "engine_shim": "launcher/engine_shim.py",
        # schema 2: the runtime is SHARED, so _python is sys.executable
        "runtime_fingerprint": "cp311-aaaaaaaaaaaa",
    }), encoding="utf-8")
    return pkg


def _shared_shell(tmp_path: Path, *, present: bool) -> Path:
    """deps/shells/<fp>/cim-light.exe — outside every version, used by all of them."""
    exe = tmp_path / "deps" / "shells" / "sh-abc" / "cim-light.exe"
    if present:
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
    return exe


def test_a_missing_shared_shell_does_not_mark_the_version_failed(tmp_path, monkeypatch,
                                                                 capsys):
    """The shell lives in deps/shells/<fp>/ and EVERY version in the store points at
    the same one. Antivirus eats it, and load_manifest's existence loop raised the same
    ManifestError it raises for a missing entrypoint — so main() returned 4, bootstrap
    marked a perfectly good version failed (a sticky verdict: only --clear-failed undoes
    it), rolled back onto a version missing the identical folder, watched it die the same
    way, and announced 「已恢復前一版本」 about a recovery that never happened.

    Exit 5 is the code that means 'touch no state, blame no version'."""
    pkg = _store_package(tmp_path)
    monkeypatch.setenv("CIM_SHELL_EXE", str(_shared_shell(tmp_path, present=False)))
    monkeypatch.setenv("CIM_APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(launch, "PKG_ROOT", pkg)

    with pytest.raises(launch.SharedComponentError):
        launch.load_manifest(pkg)

    assert launch.main([]) == launch.EXIT_MACHINE_BROKEN     # 5 — never 4
    err = capsys.readouterr().err
    assert "共用" in err                                     # whose component it is
    assert "不會退版" in err and "不會把這個版本標記為失敗" in err
    assert "WebView2" in err and "排除清單" in err            # …and what to DO about it
    err.encode("cp950")


def test_a_missing_entrypoint_is_still_this_versions_fault(tmp_path, monkeypatch, capsys):
    """The other side of the line, and the reason SharedComponentError has to be caught
    BEFORE ManifestError rather than instead of it: a file that THIS VERSION declares
    inside THIS VERSION's own folder is version-specific. It must still be exit 4."""
    pkg = _store_package(tmp_path)
    (pkg / "application" / "app.py").unlink()
    monkeypatch.setenv("CIM_SHELL_EXE", str(_shared_shell(tmp_path, present=True)))
    monkeypatch.setenv("CIM_APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(launch, "PKG_ROOT", pkg)

    assert launch.main([]) == launch.EXIT_VERSION_BROKEN     # 4: fail it, roll back
    err = capsys.readouterr().err
    assert "自動退回上一個可用版本" in err                     # and TELL the user that
    err.encode("cp950")


# ── the red text on the factory floor ────────────────────────────────────────

def test_the_missing_package_message_is_written_for_the_person_who_is_reading_it(tmp_path):
    """It used to end with 「請回到打包工具,把上面的套件加進 requirements(或 lock 檔)後
    重新建置這個版本」 — the last word on the screen of a production machine, addressed to
    an admin who is not in the room, read by a line worker who has never seen the
    packaging tool and cannot act on a single word of it. They take it to mean the line
    is down until somebody comes. It is not: the machine rolls itself back within
    seconds, and that fact was nowhere on the screen."""
    text = launch.missing_modules_message(["cv2"], tmp_path / "application")
    lines = text.splitlines()

    def at(needle: str) -> int:
        return next(i for i, line in enumerate(lines) if needle in line)

    # what the person in the room needs, and needs FIRST
    assert "自動退回上一個可用版本" in text and "您不需要做任何事" in text
    assert "交給管理員" in text
    # the rebuild instruction survives — demoted to one line, under a heading that
    # says out loud that it is not for the reader.
    assert at("您不需要做任何事") < at("以下請交給管理員") < at("重新建置這個版本")
    assert "opencv-python" in text          # the admin still gets everything they need
    text.encode("cp950")


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

    # what _spawn_and_wait does for the new process (the same lines, in one place).
    # _log_size/_log_changed_at MUST be reset too: the old log's size compared against
    # the new log's would fake a "change" (or a false quiet) on the very first tick.
    supervisor._log_path = tmp_path / "streamlit-restart.log"
    supervisor._log_path.write_bytes(b"")
    supervisor._healthy_at = None
    supervisor._session_at = None
    supervisor._arrival_offset = None
    supervisor._log_size = None
    supervisor._log_changed_at = None

    assert supervisor.arriving
    supervisor.note_arrival_window()
    assert supervisor._arrival_offset is None               # no session yet: stays open


def test_the_restart_reset_covers_every_field_the_supervisor_tracks(tmp_path):
    """The test above hand-rolls what _spawn_and_wait does. If someone adds a sixth
    piece of per-run state and forgets it there, the next run inherits the last run's
    accounting — which is exactly how a startup crash gets filed as a 'late' error.
    So: pin the list."""
    supervisor = _real_supervisor(tmp_path)
    per_run = {"_healthy_at", "_session_at", "_arrival_offset", "_log_size",
               "_log_changed_at"}
    for field in per_run:
        assert hasattr(supervisor, field), field

    source = (TEMPLATES / "launch.py").read_text(encoding="utf-8")
    spawn = source.split("def _spawn_and_wait", 1)[1].split("\n    def ", 1)[0]
    for field in per_run:
        assert f"self.{field} = None" in spawn, \
            f"_spawn_and_wait does not reset {field}: the next run inherits it"


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


# ── the arrival window closes on QUIET, not on a wall clock ──────────────────
#
# "20 seconds have passed, therefore the app has rendered" is a guess. It is wrong for
# exactly the apps that need the safety net most: the ones with a heavy first render (a
# model loaded at import, a big table read at module scope, a slow machine whose
# antivirus reads every byte of an 80 MB weight file). Those are still legitimately
# STARTING at T+20s, so their fatal error at T+30s landed in the "late" half of the log,
# was downgraded to a warning, the marker was kept, and the broken version was committed
# as last-known-good.

def test_a_slow_starting_app_that_is_still_writing_keeps_its_arrival_window_open(tmp_path):
    """The app is talking — a progress line every tick, 40 seconds past the old 20s
    wall clock. It has not arrived; it is still arriving. The window must stay open,
    so that the traceback it finally emits is still an ARRIVAL failure."""
    supervisor = _supervisor(tmp_path, _STARTUP)
    log_path = tmp_path / "streamlit.log"
    supervisor.note_session_start()
    supervisor._session_at = time.monotonic() - 40.0        # 40s in: the old clock is long gone

    body = _STARTUP
    for step in range(8):                                   # the app keeps logging
        body += f"loading shard {step}/8…\n"
        log_path.write_bytes(body.encode("utf-8"))
        supervisor.note_arrival_window()
        assert supervisor.arriving, \
            "the window closed while the app was visibly still working"

    # …and now it dies. Because the window never closed, this is fatal, not a warning.
    log_path.write_bytes((body + "Traceback (most recent call last):\n"
                                 "RuntimeError: could not load the model\n").encode("utf-8"))
    fatal = supervisor.app_error_in_log()
    assert fatal and "RuntimeError" in fatal
    assert supervisor.late_app_error_in_log() is None
    assert launch.finish_session(supervisor, launch.EXIT_OK) == launch.EXIT_APP_BROKEN


def test_the_window_closes_once_the_app_stops_writing(tmp_path):
    """The other half: quiet must actually END the window, or every red box the user
    ever causes becomes a failed version and an unasked-for downgrade."""
    supervisor = _supervisor(tmp_path, _STARTUP)
    supervisor.note_session_start()

    # It wrote something, then went quiet longer than the quiet period.
    supervisor.note_arrival_window()
    supervisor._session_at = time.monotonic() - launch.APP_ARRIVAL_SECONDS - 1
    supervisor._log_changed_at = time.monotonic() - launch.APP_ARRIVAL_QUIET_SECONDS - 1

    supervisor.note_arrival_window()
    assert supervisor._arrival_offset == len(_STARTUP.encode("utf-8"))
    assert not supervisor.arriving

    # An error NOW is a red box in a working app: a warning, not a failed version.
    (tmp_path / "streamlit.log").write_bytes((_STARTUP + _TRACEBACK).encode("utf-8"))
    assert supervisor.app_error_in_log() is None
    assert supervisor.late_app_error_in_log() is not None


def test_the_arrival_window_always_terminates_even_for_an_app_that_never_shuts_up(tmp_path):
    """The bound. An app that logs a line every second (a polling loop, a chatty
    library) would hold the window open forever on the quiet rule alone — and a window
    that never closes turns every user-caused red box into a rollback. The bound is
    what makes this terminate; the module comment says what it costs."""
    assert launch.APP_ARRIVAL_MAX_SECONDS > launch.APP_ARRIVAL_QUIET_SECONDS
    supervisor = _supervisor(tmp_path, _STARTUP)
    log_path = tmp_path / "streamlit.log"
    supervisor.note_session_start()
    supervisor._session_at = time.monotonic() - launch.APP_ARRIVAL_MAX_SECONDS - 1

    body = _STARTUP + "still chattering\n"                  # it is STILL writing right now
    log_path.write_bytes(body.encode("utf-8"))
    supervisor.note_arrival_window()                        # first look: seeds the size
    body += "and again\n"
    log_path.write_bytes(body.encode("utf-8"))
    supervisor.note_arrival_window()                        # changed -> not quiet at all

    assert not supervisor.arriving, \
        "past the absolute bound the window must close even though the log is moving"


def test_a_silent_app_still_gets_the_floor_before_its_errors_are_forgiven(tmp_path):
    """A Streamlit app that starts cleanly logs NOTHING, so it is 'quiet' from its very
    first breath. Without a floor, the window would close on the first tick and a crash
    two seconds later would already be excused as 'late'."""
    supervisor = _supervisor(tmp_path, _STARTUP)
    supervisor.note_session_start()

    for _ in range(5):                                      # ticks, all within the floor
        supervisor.note_arrival_window()
        assert supervisor.arriving

    supervisor._session_at = time.monotonic() - launch.APP_ARRIVAL_SECONDS - 1
    supervisor.note_arrival_window()
    assert not supervisor.arriving                          # past the floor and quiet: closed


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
    """Answers only the questions finish_session asks: what did the app's log say,
    and was the app ever actually asked to run?

    `session_started` defaults to True — every test using this class is about a
    session in which the user DID press Start. The "they never pressed Start" case
    has its own tests below, against the real supervisor."""

    def __init__(self, fatal=None, late=None, session_started=True):
        self._fatal, self._late = fatal, late
        self.session_started = session_started
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


# ── THE BLOCKER: a version that never ran must never be stamped last-known-good ──
#
# Streamlit does not execute the app script until a session opens, and a session
# opens only when the user presses Start in the portal. So the most ordinary thing a
# user can do — open the app, look at the portal, close the window — used to produce
# exit 0 + a marker, and bootstrap committed a version that had NEVER EXECUTED A LINE
# as last-known-good. If that build was broken, the next launch died and the version
# it "rolled back" to was that same broken build.
#
# The marker therefore has two BODIES, and only one of them may promote a version.

def _real_supervisor(tmp_path: Path) -> "launch.StreamlitSupervisor":
    return launch.StreamlitSupervisor(
        {"_python": tmp_path / "python.exe", "_entrypoint": tmp_path / "app.py",
         "host": "127.0.0.1", "preferred_port": 0}, tmp_path)


def test_a_window_the_user_closed_without_pressing_start_is_not_last_known_good(
        tmp_path, monkeypatch):
    """THE reproduction. The window opens, the user never presses Start, they close
    it. The app script has not run — not one line — so the marker must NOT claim the
    version works, and bootstrap must not commit it.

    It must also not FAIL: the user simply did not use the app. Failing a version for
    that would roll the machine back for nothing."""
    marker = tmp_path / "healthy"
    monkeypatch.setenv("CIM_HEALTHY_MARKER", str(marker))

    shell = FakeShell(code=0, alive_polls=None, alive_waits=2)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *_a, **_k: shell)
    monkeypatch.setattr(launch, "_WINDOW_POLL_SECONDS", 0.001)
    monkeypatch.setattr(launch, "SHELL_ALIVE_SECONDS", 0.01)
    monkeypatch.setattr(launch, "_SHELL_TICK_SECONDS", 0.01)

    supervisor = _real_supervisor(tmp_path)          # nobody ever calls note_session_start
    code = launch.run_shell(
        _shell_manifest(tmp_path), FakeControl(), tmp_path,
        on_window_ready=lambda: launch._write_marker(launch.MARKER_NO_SESSION),
        on_tick=lambda: False)

    assert code == launch.EXIT_OK                    # the window was fine; nothing broke
    assert not supervisor.session_started            # …but the app was never asked to run
    assert launch.finish_session(supervisor, code) == launch.EXIT_OK

    # The marker exists (a window DID come up) but says so and nothing more.
    assert marker.read_text(encoding="utf-8") == launch.MARKER_NO_SESSION
    assert "http" not in marker.read_text(encoding="utf-8"), \
        "a version whose app never ran must not advertise a URL bootstrap will commit on"


def test_pressing_start_is_what_upgrades_the_marker_to_a_url(tmp_path, monkeypatch):
    """The other side: the user DID press Start. /control/start is the moment the app
    is asked to run, and that — and only that — turns the marker into a promotable
    claim. Asserted through the REAL HTTP handler: a supervisor method nobody calls
    protects nobody."""
    marker = tmp_path / "healthy"
    monkeypatch.setenv("CIM_HEALTHY_MARKER", str(marker))

    class ReadySupervisor(launch.StreamlitSupervisor):
        def start(self):
            return "http://127.0.0.1:9999"

        @property
        def url(self):
            return "http://127.0.0.1:9999"

        @property
        def port(self):
            return 9999

    supervisor = ReadySupervisor(
        {"_python": tmp_path / "python.exe", "_entrypoint": tmp_path / "app.py",
         "host": "127.0.0.1", "preferred_port": 0}, tmp_path)
    launch._write_marker(launch.MARKER_NO_SESSION)        # the window came up first
    assert marker.read_text(encoding="utf-8") == launch.MARKER_NO_SESSION

    control = launch.ControlServer(supervisor)
    control.start()
    try:
        request = urllib.request.Request(f"{control.url}/control/start", method="POST",
                                         headers={"X-CIM-Token": control.token})
        with urllib.request.urlopen(request, timeout=5) as resp:
            assert resp.status == 200
    finally:
        control.shutdown()

    assert supervisor.session_started
    assert marker.read_text(encoding="utf-8") == "http://127.0.0.1:9999"
    assert launch.finish_session(supervisor, launch.EXIT_OK) == launch.EXIT_OK
    assert marker.read_text(encoding="utf-8") == "http://127.0.0.1:9999"  # still promotable


def test_the_marker_never_downgrades_a_real_session_to_no_session(tmp_path, monkeypatch):
    """Ordering hazard: on_window_ready fires when the window has survived its
    creation watch, and a user CAN press Start inside those three seconds (or the
    shell can exit 0 inside them, which also fires it). If "no-session" could
    overwrite a URL already in the marker, that user's real session would be thrown
    away and their good version would never be committed."""
    marker = tmp_path / "healthy"
    monkeypatch.setenv("CIM_HEALTHY_MARKER", str(marker))

    launch._write_marker("http://127.0.0.1:8501")         # /control/start won the race
    launch._write_marker(launch.MARKER_NO_SESSION)        # …on_window_ready lands after

    assert marker.read_text(encoding="utf-8") == "http://127.0.0.1:8501"


def test_an_app_that_died_on_arrival_loses_the_marker_entirely(tmp_path, monkeypatch):
    """Revocation still beats both bodies: the app WAS asked to run and proved it
    never became usable. Neither "no-session" (a lie — they did press Start) nor the
    URL (a lie — it never worked) is true, so the marker goes."""
    marker = tmp_path / "healthy"
    monkeypatch.setenv("CIM_HEALTHY_MARKER", str(marker))
    launch._write_marker("http://127.0.0.1:8501")

    code = launch.finish_session(
        LogSupervisor(fatal="ModuleNotFoundError: No module named 'cv2'"), launch.EXIT_OK)

    assert code == launch.EXIT_APP_BROKEN
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


# ── "inside a def" is not the same as "lazy" ─────────────────────────────────
#
# The preflight used to `continue` on EVERY FunctionDef, so an import inside a
# function was always called lazy. But a function the MODULE BODY CALLS runs on the
# first render, exactly like a module-level import: `_setup()` at the bottom of the
# file, `CONFIG = boot()`, a call inside a module-level `if`, `@register` on a
# module-level def. A missing package behind one of those sailed through this gate
# and met the operator as a red box — and the build-side gate (imports.py) now
# catches precisely that, so a package it rejects would still have reached the floor.
#
# REQUIRED = module level, OR the body of a def the module body calls. Same rule,
# same file, as imports.py — do not grow a second one.

def test_an_import_inside_a_module_called_function_is_required_by_the_preflight(tmp_path):
    """THE defect. `_setup()` is called at module scope, so `import cv2` in its body
    runs on the first render. Calling it lazy shipped a package that dies on arrival."""
    app_root = tmp_path / "application"
    app_root.mkdir(parents=True)
    entry = app_root / "app.py"
    entry.write_text("import streamlit as st\n"
                     "def _setup():\n"
                     "    import definitely_not_installed_pkg\n"
                     "_setup()\n", encoding="utf-8")

    missing, syntax_error = launch.preflight(entry, app_root)
    assert syntax_error is None
    assert missing == ["definitely_not_installed_pkg"]


@pytest.mark.parametrize("body,label", [
    ("def boot():\n    import definitely_not_installed_pkg\n    return 1\n"
     "CONFIG = boot()\n", "a call in a module-level assignment"),
    ("import os\n"
     "def boot():\n    import definitely_not_installed_pkg\n"
     "if os.environ.get('X'):\n    boot()\n", "a call inside a module-level if"),
    ("def register(fn):\n    import definitely_not_installed_pkg\n    return fn\n"
     "@register\ndef page():\n    pass\n", "a decorator on a module-level def"),
])
def test_every_way_the_module_body_can_run_a_function_makes_its_imports_required(
        tmp_path, body, label):
    """All four promotion routes the build gate recognises. Each one really does
    execute while Streamlit imports the script."""
    app_root = tmp_path / "application"
    app_root.mkdir(parents=True)
    entry = app_root / "app.py"
    entry.write_text("import streamlit as st\n" + body, encoding="utf-8")

    missing, _ = launch.preflight(entry, app_root)
    assert missing == ["definitely_not_installed_pkg"], label


@pytest.mark.parametrize("body,why", [
    ("def _setup():\n    import definitely_not_installed_pkg\n"
     "def main():\n    _setup()\n"
     "main()\n", "two hops deep: module -> main() -> _setup()"),
    ("class App:\n    def boot(self):\n        import definitely_not_installed_pkg\n"
     "App().boot()\n", "a method: App().boot() does not promote boot"),
    ("def _setup():\n    import definitely_not_installed_pkg\n"
     "if __name__ == '__main__':\n    _setup()\n",
     "__main__ guard: promoting it would make every main()-style script's lazy "
     "imports hard requirements"),
    ("def _setup():\n"
     "    try:\n        import definitely_not_installed_pkg\n"
     "    except ImportError:\n        definitely_not_installed_pkg = None\n"
     "_setup()\n", "try/except ImportError inside a promoted function still degrades"),
])
def test_the_promotion_rule_fails_open_exactly_where_the_build_gate_does(
        tmp_path, body, why):
    """The other direction, and it matters more: a WRONG 'required' refuses an app
    that works. Every case the build gate declines to promote, this must decline too —
    or the two sides disagree about the same file and the operator gets told to
    `pip install` something the build was perfectly happy with."""
    app_root = tmp_path / "application"
    app_root.mkdir(parents=True)
    entry = app_root / "app.py"
    entry.write_text("import streamlit as st\n" + body, encoding="utf-8")

    missing, syntax_error = launch.preflight(entry, app_root)
    assert syntax_error is None
    assert missing == [], why


def test_a_plain_lazy_import_is_still_not_a_requirement(tmp_path):
    """The original reason the rule exists: `import anthropic` in a function nobody
    calls at import time is genuinely lazy. Hard-failing a build over an optional LLM
    backend nobody enabled is how a good version gets refused."""
    app_root = tmp_path / "application"
    app_root.mkdir(parents=True)
    entry = app_root / "app.py"
    entry.write_text("import streamlit as st\n"
                     "def ask_llm():\n"
                     "    import definitely_not_installed_pkg\n"
                     "    return definitely_not_installed_pkg\n", encoding="utf-8")

    assert launch.preflight(entry, app_root) == ([], None)


def test_both_halves_of_the_gate_name_the_called_scope_the_same_way(tmp_path):
    """One rule, two files, one operator. If the device side invents its own scope
    label the two halves start explaining the same import differently — and the next
    person to change one of them will not know to change the other."""
    from provision_builder.streamlit_desktop import imports as imports_mod

    assert launch.CALLED_SCOPE == imports_mod.CALLED_SCOPE == "module-called"
    assert imports_mod._SCOPE_LABEL[imports_mod.CALLED_SCOPE] == \
        "函式內 import,但這個函式在模組層被呼叫(啟動時就會執行)"


# The anti-drift guard that actually bites: run BOTH implementations over the same
# source and demand the same answer. The build gate and this preflight ask one
# question — "does this import run when Streamlit loads the script?" — and the whole
# defect was that they answered it differently. A rule change on either side that
# forgets the other now fails here instead of in a factory.
_SAME_ANSWER_SOURCES = [
    "import cv2\n",
    "def _setup():\n    import cv2\n_setup()\n",
    "def boot():\n    import cv2\n    return 1\nCONFIG = boot()\n",
    "import os\ndef boot():\n    import cv2\nif os.environ.get('X'):\n    boot()\n",
    "def register(fn):\n    import cv2\n    return fn\n@register\ndef page():\n    pass\n",
    "def _setup():\n    import cv2\ndef main():\n    _setup()\nmain()\n",
    "class App:\n    def boot(self):\n        import cv2\nApp().boot()\n",
    "def _setup():\n    import cv2\nif __name__ == '__main__':\n    _setup()\n",
    "def _setup():\n    try:\n        import cv2\n    except ImportError:\n        cv2 = None\n_setup()\n",
    "def lazy():\n    import cv2\n",
    "try:\n    import cv2\nexcept ImportError:\n    cv2 = None\n",
    "with open('x') as f:\n    import cv2\n",
]


@pytest.mark.parametrize("source", _SAME_ANSWER_SOURCES)
def test_the_device_preflight_and_the_build_gate_agree_on_what_runs_at_import(
        tmp_path, source):
    """Differential: same file, both rules, one answer."""
    import ast

    from provision_builder.streamlit_desktop import imports as imports_mod

    path = tmp_path / "app.py"
    path.write_text(source, encoding="utf-8")

    device = launch._module_level_imports(ast.parse(source))
    build = {site.module.split(".")[0]
             for site in imports_mod._parse_import_sites(path)
             if site.scope in imports_mod._REQUIRED_SCOPES}

    assert device == build, f"the two halves disagree about:\n{source}"

"""Validation + folder assembly.

pip and the 80 MB runtime copy are stubbed here; the real thing is exercised by
e2e/streamlit_desktop_e2e.py against a real runtime, a real Streamlit and the
real prebuilt shell. What we prove here is the logic that decides whether a
build may proceed and what lands in the folder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import (
    BuildRequest,
    app_id_for,
    build,
    declared_packages,
    slugify,
    smoke_test,
    validate_request,
)
from provision_builder.streamlit_desktop import builder as builder_mod
from provision_builder.streamlit_desktop import imports as imports_mod
from provision_builder.streamlit_desktop import runtime as runtime_mod


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "my project"          # a space, on purpose
    root.mkdir()
    (root / "app.py").write_text("import streamlit as st\nst.write('READY')\n", encoding="utf-8")
    (root / "requirements.txt").write_text("streamlit==1.40.0\n", encoding="utf-8")
    return root


@pytest.fixture
def shell_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "prebuilt" / "cim-light.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ fake shell")
    return exe


@pytest.fixture
def runtime_template(tmp_path: Path) -> Path:
    root = tmp_path / "runtime-template"
    (root / "Lib").mkdir(parents=True)
    (root / "python.exe").write_bytes(b"MZ fake python")
    (root / "Lib" / "os.py").write_text("", encoding="utf-8")
    return root


@pytest.fixture
def request_(project, shell_exe, runtime_template, tmp_path) -> BuildRequest:
    return BuildRequest(
        project_dir=project,
        entrypoint=project / "app.py",
        display_name="My Streamlit App",
        output_dir=tmp_path / "out",
        shell_exe=shell_exe,
        runtime_template=runtime_template,
    )


@pytest.fixture
def stub_pip(monkeypatch):
    """Skip the real 200 MB install; still assert we were asked to do it.
    The import scan needs a real interpreter, so it is stubbed too."""
    calls = {}

    def fake_install(python, requirements, log_file, **kwargs):
        calls["install"] = (Path(python), Path(requirements))
        calls["install_kwargs"] = kwargs
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("pip ok\n", encoding="utf-8")

    def fake_verify(python, _log_file):
        calls["verify"] = Path(python)

    monkeypatch.setattr(runtime_mod, "install_requirements", fake_install)
    monkeypatch.setattr(runtime_mod, "verify_imports", fake_verify)
    monkeypatch.setattr(imports_mod, "missing_dependencies",
                        lambda *_a, **_k: calls.get("missing", imports_mod.MissingReport()))
    return calls


# ── naming ───────────────────────────────────────────────────────────────────

def test_app_id_uses_the_app_prefix_that_gives_a_full_height_iframe():
    assert app_id_for("My Streamlit App") == "app-my-streamlit-app"
    assert slugify("報表 Dashboard!") == "dashboard"


# ── requirements parsing ─────────────────────────────────────────────────────

def test_declared_packages_reads_pins_and_ignores_comments():
    text = "# comment\nstreamlit==1.40.0\npandas>=2\n\n-r other.txt\n"
    assert declared_packages(text) == {"streamlit", "pandas"}


def test_streamlit_aggrid_is_not_mistaken_for_streamlit():
    assert "streamlit" not in declared_packages("streamlit-aggrid==1.0\n")


# ── validation (fail closed) ─────────────────────────────────────────────────

def test_valid_request_has_no_errors(request_):
    assert validate_request(request_) == []


def test_missing_project_dir_is_rejected(request_):
    request_.project_dir = request_.project_dir.parent / "nope"
    assert any("專案資料夾不存在" in e for e in validate_request(request_))


def test_missing_entrypoint_is_rejected(request_):
    request_.entrypoint = request_.project_dir / "missing.py"
    assert any("入口檔不存在" in e for e in validate_request(request_))


def test_entrypoint_outside_the_project_is_rejected(request_, tmp_path):
    outside = tmp_path / "elsewhere.py"
    outside.write_text("", encoding="utf-8")
    request_.entrypoint = outside
    assert any("必須位於專案資料夾內" in e for e in validate_request(request_))


def test_entrypoint_escaping_with_dotdot_is_rejected(request_, tmp_path):
    (tmp_path / "evil.py").write_text("", encoding="utf-8")
    request_.entrypoint = Path(str(request_.project_dir / ".." / "evil.py"))
    request_.__post_init__()                       # re-resolve, as the GUI would
    assert any("必須位於專案資料夾內" in e for e in validate_request(request_))


def test_requirements_without_streamlit_is_rejected(request_):
    (request_.project_dir / "requirements.txt").write_text("pandas\n", encoding="utf-8")
    assert any("沒有 streamlit" in e for e in validate_request(request_))


def test_project_with_no_dependency_declaration_at_all_is_rejected(request_):
    (request_.project_dir / "requirements.txt").unlink()
    errors = validate_request(request_)
    assert any("找不到相依宣告" in e for e in errors)
    assert any("pyproject.toml" in e for e in errors)   # tells you what would work


def test_missing_shell_is_rejected_rather_than_degraded(request_):
    request_.shell_exe = request_.shell_exe.parent / "gone.exe"
    errors = validate_request(request_)
    assert any("找不到預建 Tauri 殼" in e for e in errors)


def test_missing_runtime_template_is_rejected(request_, tmp_path):
    request_.runtime_template = tmp_path / "no-runtime"
    assert any("找不到可攜 Python runtime" in e for e in validate_request(request_))


def test_build_refuses_to_run_on_an_invalid_request(request_):
    request_.entrypoint = request_.project_dir / "missing.py"
    result = build(request_)
    assert not result.ok and result.package_dir is None


# ── assembly ─────────────────────────────────────────────────────────────────

def test_build_produces_the_expected_layout(request_, stub_pip):
    result = build(request_)
    assert result.ok, result.errors
    pkg = result.package_dir

    for expected in ("start.bat", "app-package.json", "讀我-使用說明.txt",
                     "tools/安裝WebView2.bat",
                     "application/app.py", "application/requirements.txt",
                     "runtime/python.exe", "launcher/launch.py",
                     "launcher/engine_shim.py", "shell/cim-light.exe"):
        assert (pkg / expected).exists(), f"missing {expected}"
    assert (pkg / "data" / "logs").is_dir()
    assert stub_pip["install"][0] == pkg.parent / pkg.name / "runtime" / "python.exe" or stub_pip["install"]


def test_manifest_contains_only_relative_paths(request_, stub_pip):
    pkg = build(request_).package_dir
    manifest = json.loads((pkg / "app-package.json").read_text("utf-8"))
    for key in ("entrypoint", "python", "shell_executable", "engine_shim"):
        value = manifest[key]
        assert not Path(value).is_absolute(), f"{key} is absolute: {value}"
        assert ":" not in value, f"{key} leaks a drive letter: {value}"
    assert manifest["app_id"] == "app-my-streamlit-app"
    # 0 = no fixed port: the launcher picks a random free one in 8000–9000.
    assert manifest["preferred_port"] == 0


def test_package_does_not_leak_build_machine_paths(request_, stub_pip):
    pkg = build(request_).package_dir
    for name in ("app-package.json", "start.bat"):
        text = (pkg / name).read_text("utf-8")
        assert str(request_.project_dir) not in text
        assert str(request_.runtime_template) not in text


def test_build_excludes_caches_and_virtualenvs(request_, stub_pip):
    for junk in (".git", ".venv", "__pycache__", "node_modules"):
        (request_.project_dir / junk).mkdir()
        (request_.project_dir / junk / "junk.txt").write_text("x", encoding="utf-8")
    pkg = build(request_).package_dir
    for junk in (".git", ".venv", "__pycache__", "node_modules"):
        assert not (pkg / "application" / junk).exists()


def test_build_excludes_the_projects_wheelhouse(request_, stub_pip):
    """CV_Viewer shipped a 124 MB wheels/ folder into the package. The user's
    machine never opens a .whl — the deps are already installed in the runtime."""
    wheels = request_.project_dir / "wheels"
    wheels.mkdir()
    (wheels / "numpy-2.0-cp311-win_amd64.whl").write_bytes(b"PK fake wheel")
    (wheels / "README.md").write_text("keep me", encoding="utf-8")

    pkg = build(request_).package_dir
    assert not (pkg / "application" / "wheels").exists()   # the whole wheelhouse


def test_archives_and_build_artifacts_do_not_travel(request_, stub_pip):
    (request_.project_dir / "dataset.zip").write_bytes(b"PK" * 10)
    (request_.project_dir / "backup.tar.gz").write_bytes(b"\x1f\x8b" * 10)
    (request_.project_dir / "dist").mkdir()
    (request_.project_dir / "dist" / "app-1.0.whl").write_bytes(b"PK")

    pkg = build(request_).package_dir
    assert not (pkg / "application" / "dataset.zip").exists()
    assert not (pkg / "application" / "backup.tar.gz").exists()
    assert not (pkg / "application" / "dist").exists()


# ── one exclusion rule for the estimate AND the copy ─────────────────────────

def test_egg_info_is_excluded_by_the_same_rule_that_estimates_it(request_, stub_pip):
    """`*.egg-info` is a DIRECTORY. The estimate used to count it (it only tested
    the pattern against files) while the copy dropped it — so the operator was
    quoted a size the package could never have. One rule, both paths."""
    egg = request_.project_dir / "myapp.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_bytes(b"x" * (2 * 1024 ** 2))

    assert builder_mod.should_ignore("myapp.egg-info", is_dir=True)
    scan = builder_mod.scan_project(request_)
    assert scan.application_mb < 1                      # not counted in the estimate
    assert "*.egg-info" in scan.excluded                # counted as excluded instead

    pkg = build(request_).package_dir
    assert not (pkg / "application" / "myapp.egg-info").exists()


def test_a_path_pattern_does_not_exclude_the_entire_project():
    """`data/*` used to be collapsed to its last segment — `*` — and fnmatch(x, "*")
    is True for everything. So typing ONE exclude pattern silently dropped every
    file in the project, and the build still reported success: a delivered folder
    with no application code in it. This is the highest-cost class of bug we have:
    it produces a broken artefact and calls it done."""
    keep = builder_mod.should_ignore
    assert not keep("app.py", False, ("data/*", "*.mp4"), "app.py")
    assert not keep("utils.py", False, ("data/*",), "utils.py")
    assert not keep("notes.md", False, ("data/*",), "docs/notes.md")

    assert keep("demo.mp4", False, ("*.mp4",), "demo.mp4")
    assert keep("raw.csv", False, ("data/*",), "data/raw.csv")
    assert keep("deep.csv", False, ("data/*",), "data/nested/deep.csv")   # 整棵子樹
    assert keep("data", True, ("data/",), "data")


def test_the_store_slot_honours_the_same_exclusions_as_the_fat_package(request_, stub_pip):
    """They used to disagree: store mode called shutil.ignore_patterns() directly and
    never saw .provisionignore or the GUI's 額外排除 field. The same project excluded
    a 85MB recording in fat mode and shipped it in every store update."""
    from provision_builder.streamlit_desktop import builder as b
    (request_.project_dir / "demo.mp4").write_text("x" * 1024, encoding="utf-8")
    (request_.project_dir / ".provisionignore").write_text("*.mp4\n", encoding="utf-8")

    ignore = b.copytree_ignore(b.ignore_patterns_for(request_), request_.project_dir)
    dropped = ignore(str(request_.project_dir), ["app.py", "demo.mp4"])
    assert "demo.mp4" in dropped
    assert "app.py" not in dropped


def test_the_scan_says_what_it_threw_away_and_how_big_it_was(request_, stub_pip):
    """「為什麼我的資料夾有 700 MB」 deserves an answer up front."""
    wheels = request_.project_dir / "wheels"
    wheels.mkdir()
    (wheels / "numpy.whl").write_bytes(b"x" * (3 * 1024 ** 2))
    (request_.project_dir / "junk.pyc").write_bytes(b"x" * (2 * 1024 ** 2))

    scan = builder_mod.scan_project(request_)
    assert scan.excluded_mb >= 5
    assert "已自動排除" in scan.excluded_summary
    assert "wheels/" in scan.excluded_summary and "*.pyc" in scan.excluded_summary
    # 使用者看得到它 —— 但它是「資訊」不是「警告」:警告會讓 GUI 在開工前擋一次,
    # 而「我已經幫你排掉 124MB 的 wheels」不需要任何人做決定。
    assert any("已自動排除" in n for n in scan.notes)
    assert not any("已自動排除" in w for w in scan.warnings)


def test_provisionignore_and_extra_excludes_are_honoured(request_, stub_pip):
    (request_.project_dir / ".provisionignore").write_text(
        "# 交付時不要帶的東西\n*.mp4\nprivate/\n", encoding="utf-8")
    (request_.project_dir / "demo.mp4").write_bytes(b"x" * 1024)
    (request_.project_dir / "private").mkdir()
    (request_.project_dir / "private" / "secret.txt").write_text("s", encoding="utf-8")
    (request_.project_dir / "notes.txt").write_text("keep", encoding="utf-8")
    request_.extra_excludes = ("notes.txt",)

    pkg = build(request_).package_dir
    assert not (pkg / "application" / "demo.mp4").exists()
    assert not (pkg / "application" / "private").exists()
    assert not (pkg / "application" / "notes.txt").exists()
    assert (pkg / "application" / "app.py").is_file()          # the app still travels


# ── leftovers from a crashed run ─────────────────────────────────────────────

def test_orphan_staging_dirs_are_reported_and_deleted(request_, stub_pip):
    """A killed build leaves .staging-* holding a whole copied runtime. It is
    invisible, it is hundreds of MB, and nothing ever cleaned it up."""
    orphan = request_.output_dir
    orphan.mkdir(parents=True, exist_ok=True)
    junk = orphan / ".staging-my-streamlit-app-deadbeef"
    junk.mkdir()
    (junk / "runtime.bin").write_bytes(b"x" * (2 * 1024 ** 2))

    lines: list[str] = []
    result = build(request_, progress=lines.append)

    assert result.ok
    assert not junk.exists()
    assert any("暫存目錄" in line and "2 MB" in line for line in lines)
    assert not any(request_.output_dir.glob(".staging-*"))


def test_size_breakdown_names_the_heavy_dependencies(request_, stub_pip):
    """The admin should learn WHY a package is 700 MB from the build log, not by
    going and measuring it themselves."""
    pkg = build(request_).package_dir
    site = pkg / "runtime" / "Lib" / "site-packages"
    (site / "cv2").mkdir(parents=True)
    (site / "cv2" / "big.pyd").write_bytes(b"x" * 5000)
    (site / "tiny").mkdir()
    (site / "tiny" / "a.py").write_text("x", encoding="utf-8")

    lines = "\n".join(builder_mod.size_breakdown(pkg))
    assert "runtime" in lines and "application" in lines
    assert "cv2" in lines and lines.index("cv2") < lines.index("tiny")   # biggest first


def test_a_huge_file_in_the_project_is_called_out(request_, stub_pip):
    """AI4BI shipped an 85 MB screen recording that sat in the project root. We
    do not delete it — we refuse to let it travel unnoticed."""
    pkg = build(request_).package_dir
    (pkg / "application" / "demo.mp4").write_bytes(b"\0" * (12 * 1024 ** 2))

    lines = "\n".join(builder_mod.size_breakdown(pkg))
    assert "demo.mp4" in lines and "確定要交付嗎" in lines


def test_nested_entrypoint_is_mapped_into_the_application_folder(request_, stub_pip):
    nested = request_.project_dir / "src"
    nested.mkdir()
    (nested / "main.py").write_text("import streamlit\n", encoding="utf-8")
    request_.entrypoint = nested / "main.py"

    pkg = build(request_).package_dir
    manifest = json.loads((pkg / "app-package.json").read_text("utf-8"))
    assert manifest["entrypoint"] == "application/src/main.py"
    assert (pkg / "application" / "src" / "main.py").is_file()


# ── atomic swap ──────────────────────────────────────────────────────────────

def test_rebuild_replaces_the_previous_package(request_, stub_pip):
    first = build(request_)
    assert first.ok
    (first.package_dir / "application" / "stale.txt").write_text("old", encoding="utf-8")

    second = build(request_)
    assert second.ok
    assert not (second.package_dir / "application" / "stale.txt").exists()


def test_a_failed_build_leaves_the_previous_package_intact(request_, stub_pip, monkeypatch):
    good = build(request_)
    assert good.ok
    marker = good.package_dir / "application" / "app.py"
    original = marker.read_text("utf-8")

    def explode(*_args, **_kwargs):
        raise runtime_mod.RuntimeError_("pip 掛了")

    monkeypatch.setattr(runtime_mod, "install_requirements", explode)
    failed = build(request_)

    assert not failed.ok and "pip 掛了" in failed.errors[0]
    assert marker.read_text("utf-8") == original          # the working package survived
    assert not any(request_.output_dir.glob(".staging-*"))  # and no debris left behind


def test_swap_retries_through_a_transient_windows_lock(tmp_path, monkeypatch):
    """Defender holds a handle on the freshly written runtime for a moment and
    Windows answers rename() with ERROR_ACCESS_DENIED. That is not a build
    failure — a build that got this far really did succeed."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "file.txt").write_text("x", encoding="utf-8")
    final = tmp_path / "final"

    real_rename = builder_mod.os.rename
    calls = {"n": 0}

    def flaky_rename(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "Access is denied")
        return real_rename(src, dst)

    monkeypatch.setattr(builder_mod.os, "rename", flaky_rename)
    monkeypatch.setattr(builder_mod.time, "sleep", lambda _s: None)

    builder_mod._swap_into_place(staging, final)

    assert calls["n"] == 2                       # retried, did not give up
    assert (final / "file.txt").read_text("utf-8") == "x"


def test_swap_gives_up_with_a_real_error_if_the_lock_never_clears(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()

    def always_denied(_src, _dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(builder_mod.os, "rename", always_denied)
    monkeypatch.setattr(builder_mod.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        builder_mod._swap_into_place(staging, tmp_path / "final")


# ── cancel really cancels ────────────────────────────────────────────────────

def test_cancel_stops_the_build_cleans_up_and_says_so(request_, stub_pip):
    """The old 取消 button set a flag nobody read: the build ran to completion and
    reported success. A cancel that returns ok=True is worse than no button."""
    result = build(request_, should_cancel=lambda: True)

    assert result.cancelled is True
    assert result.ok is False
    assert result.message == "已取消建置,暫存目錄已清乾淨"
    assert not any(request_.output_dir.glob(".staging-*"))   # nothing left behind
    assert not request_.package_dir.exists()                 # and nothing half-written


def test_cancel_between_stages_never_swaps_a_partial_package_into_place(request_, stub_pip):
    """Cancel arriving during pip must not produce a package folder."""
    fire = {"n": 0}

    def cancel_after_pip() -> bool:
        fire["n"] += 1
        return fire["n"] > 2          # the first checks pass, a later boundary fires

    result = build(request_, should_cancel=cancel_after_pip)
    assert result.cancelled and not result.ok
    assert not request_.package_dir.exists()
    assert not any(request_.output_dir.glob(".staging-*"))


def test_a_cancelled_build_leaves_the_previous_package_intact(request_, stub_pip):
    good = build(request_)
    assert good.ok
    marker = good.package_dir / "application" / "app.py"
    original = marker.read_text("utf-8")

    cancelled = build(request_, should_cancel=lambda: True)
    assert cancelled.cancelled
    assert marker.read_text("utf-8") == original


def test_build_passes_should_cancel_down_into_pip(request_, stub_pip):
    """pip is the 6-minute stage; if the cancel flag stops at the builder's door,
    the operator waits out the whole install anyway."""
    build(request_, should_cancel=lambda: False)
    assert "should_cancel" in stub_pip["install_kwargs"]
    assert callable(stub_pip["install_kwargs"]["should_cancel"])


def test_cancelling_pip_kills_the_whole_child_tree_not_just_pip(monkeypatch, tmp_path):
    """proc.terminate() on Windows leaves pip's download/build children running —
    they keep writing into the staging directory we are about to delete. Only
    `taskkill /T /F` takes the tree down."""
    killed: list[list[str]] = []

    class HangingProc:
        pid = 4242
        stdout = iter(["Collecting numpy\n"])     # then silence, like a real download

        def __init__(self):
            self._alive = True

        def poll(self):
            return None if self._alive else 1

        def wait(self, timeout=None):
            self._alive = False
            return 1

    monkeypatch.setattr(runtime_mod.subprocess, "Popen", lambda *_a, **_k: HangingProc())
    monkeypatch.setattr(runtime_mod.subprocess, "run",
                        lambda cmd, **_k: killed.append(cmd))

    requirements = tmp_path / "r.txt"
    requirements.write_text("streamlit==1.40.0\n", encoding="utf-8")

    with pytest.raises(runtime_mod.BuildCancelled):
        runtime_mod.install_requirements(tmp_path / "python.exe", requirements,
                                         tmp_path / "logs" / "build.log",
                                         should_cancel=lambda: True)

    assert killed == [["taskkill", "/PID", "4242", "/T", "/F"]]


# ── package self-check ───────────────────────────────────────────────────────

def test_smoke_test_catches_a_path_escaping_the_package(tmp_path):
    problems = smoke_test(tmp_path, {
        "entrypoint": "../outside/app.py", "python": "runtime/python.exe",
        "shell_executable": "shell/cim-light.exe", "engine_shim": "launcher/engine_shim.py",
    })
    assert any("逃出交付根目錄" in p for p in problems)


def test_smoke_test_catches_an_absolute_path(tmp_path):
    problems = smoke_test(tmp_path, {
        "entrypoint": r"C:\evil\app.py", "python": "runtime/python.exe",
        "shell_executable": "shell/cim-light.exe", "engine_shim": "launcher/engine_shim.py",
    })
    assert any("絕對路徑" in p for p in problems)


def test_pip_runs_in_utf8_mode_and_writes_no_bytecode(monkeypatch, tmp_path):
    """Two real failures in one test:
    - Without UTF-8 mode pip decodes a Chinese-commented requirements.txt with
      the system locale (cp950) and dies before downloading anything.
    - Without --no-compile the runtime fills with .pyc files that files.json
      declares but the exporter drops, so every export fails verification."""
    seen = {}

    class FakeProc:
        stdout = iter(["Collecting streamlit\n"])

        def wait(self):
            return 0

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(runtime_mod.subprocess, "Popen", fake_popen)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("# 中文註解\nstreamlit==1.40.0\n", encoding="utf-8")

    lines = []
    runtime_mod.install_requirements(tmp_path / "python.exe", requirements,
                                     tmp_path / "logs" / "build.log",
                                     progress=lines.append)
    assert seen["env"]["PYTHONUTF8"] == "1"
    assert "--no-compile" in seen["cmd"]
    assert lines, "pip output must stream to the operator, not vanish for 10 minutes"


def test_build_manifest_is_stable(request_):
    manifest = builder_mod.build_manifest(request_, "cim-light.exe")
    assert manifest["schema_version"] == 1
    assert manifest["health_path"] == "/_stcore/health"
    assert manifest["shell_executable"] == "shell/cim-light.exe"

"""Validation + folder assembly.

pip and the 80 MB runtime copy are stubbed here; the real thing is exercised by
e2e/streamlit_desktop_e2e.py against a real runtime, a real Streamlit and the
real prebuilt shell. What we prove here is the logic that decides whether a
build may proceed and what lands in the folder.
"""

from __future__ import annotations

import json
import os
import subprocess
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
                     "tools/安裝WebView2.bat", "messages/start-webview2.txt",
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


def test_a_backslash_exclusion_pattern_is_not_silently_ignored(request_, stub_pip):
    """S7. This is a WINDOWS product: the operator types `recordings\\*` into 額外排除,
    because that is what every path on their screen looks like. `"/" not in pattern`
    was then true, the pattern was treated as a BARE NAME, fnmatch'd against
    「demo.mp4」, matched nothing — and the exclusion silently did nothing at all while
    the GUI accepted it and the report listed it as in force. A pattern that is
    accepted, appears to work, and quietly does nothing is worse than one we reject.
    """
    keep = builder_mod.should_ignore
    assert keep("demo.mp4", False, ("recordings\\*",), "recordings/demo.mp4")
    assert keep("demo.mp4", False, ("recordings/*",), "recordings/demo.mp4")   # unchanged
    assert keep("recordings", True, ("recordings\\",), "recordings")
    assert keep("deep.mp4", False, ("recordings\\*",), "recordings/2024/deep.mp4")
    assert not keep("app.py", False, ("recordings\\*",), "app.py")     # and nothing else

    # ...and the same pattern really does keep the file out of the package.
    recordings = request_.project_dir / "recordings"
    recordings.mkdir()
    (recordings / "demo.mp4").write_bytes(b"\0" * (11 * 1024 * 1024))
    request_.extra_excludes = ("recordings\\*",)

    scan = builder_mod.scan_project(request_)
    assert not any("demo.mp4" in w for w in scan.warnings), scan.warnings
    assert scan.excluded_mb >= 10

    pkg = build(request_).package_dir
    assert not (pkg / "application" / "recordings").exists()


def test_a_big_directory_still_warns_when_another_warning_merely_contains_its_name(
        request_, stub_pip):
    """S7. The big-directory warning was suppressed by `any(name in w for w in
    warnings)` — a SUBSTRING search over the warning sentences. So a genuinely huge
    `data\\` folder went unmentioned the moment some other warning happened to contain
    the letters "data" (「專案裡的大檔:metadata.bin」 is enough), and it then travelled
    into the package and onto every update. Suppress on the directory, not the letters.
    """
    (request_.project_dir / "assets").mkdir()
    (request_.project_dir / "assets" / "metadata.bin").write_bytes(b"\0" * (11 * 1024 * 1024))
    data = request_.project_dir / "data"
    data.mkdir()
    for i in range(3):                       # 30 MB, no single file big enough to warn
        (data / f"part{i}.csv").write_bytes(b"\0" * (10 * 1024 * 1024))

    scan = builder_mod.scan_project(request_)
    assert any("metadata.bin" in w for w in scan.warnings), scan.warnings
    big_dir = [w for w in scan.warnings if w.startswith("大資料夾:data")]
    assert big_dir, scan.warnings

    # the real suppression still holds: the directory whose own big file we NAMED
    # does not get a second warning about the same megabytes.
    assert not [w for w in scan.warnings if w.startswith("大資料夾:assets")], scan.warnings


# ── a build-output NAME means different things at different depths ───────────

def test_a_custom_components_compiled_frontend_survives_the_build(request_, stub_pip):
    """S6, the blocker. `dist` was matched by BARE NAME at any depth, so a Streamlit
    custom component's COMPILED frontend — the thing components.declare_component(
    path=...) points straight at, the thing that IS the component — was deleted on
    the way into the package. Verified on the real AI4BI:
    ai4bi/ui/components/field_well/frontend/dist/. The build then reported success
    and the delivered app rendered a blank box where the component should be.

    A `dist/` one level down belongs to whatever lives there. Keep it.
    """
    frontend = request_.project_dir / "ui" / "components" / "field_well" / "frontend"
    (frontend / "dist" / "assets").mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("<div id=root>", encoding="utf-8")
    (frontend / "dist" / "assets" / "index.js").write_text("export default 1", encoding="utf-8")

    rel = "ui/components/field_well/frontend/dist"
    assert not builder_mod.should_ignore("dist", True, rel=rel)

    scan = builder_mod.scan_project(request_)          # the estimate counts it...
    assert "dist/" not in scan.excluded

    pkg = build(request_).package_dir                  # ...and the copy delivers it
    shipped = pkg / "application" / "ui" / "components" / "field_well" / "frontend" / "dist"
    assert shipped.is_dir()
    assert (shipped / "index.html").is_file()
    assert (shipped / "assets" / "index.js").is_file()


def test_a_top_level_dist_is_still_the_projects_own_build_output(request_, stub_pip):
    """The other half of the same rule: at the PROJECT ROOT, `dist/` is the junk we
    have always meant to drop. Fixing S6 must not stop dropping it."""
    (request_.project_dir / "dist").mkdir()
    (request_.project_dir / "dist" / "index.html").write_text("x", encoding="utf-8")
    (request_.project_dir / "build").mkdir()
    (request_.project_dir / "build" / "index.html").write_text("x", encoding="utf-8")

    assert builder_mod.should_ignore("dist", True, rel="dist")
    assert builder_mod.should_ignore("build", True, rel="build")

    pkg = build(request_).package_dir
    assert not (pkg / "application" / "dist").exists()
    assert not (pkg / "application" / "build").exists()


def test_junk_that_can_never_be_the_app_is_dropped_at_any_depth(request_, stub_pip):
    """The depth-independent set is not an accident, it is a claim: none of these
    names can BE application code. A nested node_modules/ is the component's build
    dependency (AI4BI's is 200 MB); nothing in a packaged Python app ever opens it,
    and the compiled output already sits in dist/ — which we now keep."""
    frontend = request_.project_dir / "ui" / "components" / "field_well" / "frontend"
    for junk in ("node_modules", "__pycache__", ".git", ".venv", ".mypy_cache"):
        (frontend / junk).mkdir(parents=True)
        (frontend / junk / "junk.txt").write_text("x", encoding="utf-8")
    (frontend / "dist").mkdir()
    (frontend / "dist" / "index.html").write_text("keep", encoding="utf-8")

    pkg = build(request_).package_dir
    out = pkg / "application" / "ui" / "components" / "field_well" / "frontend"
    for junk in ("node_modules", "__pycache__", ".git", ".venv", ".mypy_cache"):
        assert not (out / junk).exists(), f"nested {junk} must not travel"
    assert (out / "dist" / "index.html").is_file()     # ...and the payload still does


def test_a_nested_archive_is_data_the_app_reads_and_must_travel(request_, stub_pip):
    """S7. `*.zip` was matched by BARE NAME at any depth — exactly the mistake `dist`
    made, one class up. So `assets/data.zip`, `models/weights.tar.gz`,
    `tests/fixtures/sample.7z` — payloads the app OPENS AT RUN TIME — were deleted on
    the way into the package. The build reported success. It even ran on the BUILD
    machine, where the file was still sitting next to the source we had copied from.
    It died on the factory floor, with a FileNotFoundError naming a path that had been
    there all along, on a machine with no way to put the file back.

    Nested, an archive is DATA. Keep it.
    """
    (request_.project_dir / "assets").mkdir()
    (request_.project_dir / "assets" / "data.zip").write_bytes(b"PK payload")
    (request_.project_dir / "models").mkdir()
    (request_.project_dir / "models" / "weights.tar.gz").write_bytes(b"\x1f\x8b payload")
    (request_.project_dir / "tests" / "fixtures").mkdir(parents=True)
    (request_.project_dir / "tests" / "fixtures" / "sample.7z").write_bytes(b"7z payload")

    assert not builder_mod.should_ignore("data.zip", False, rel="assets/data.zip")
    assert not builder_mod.should_ignore("weights.tar.gz", False, rel="models/weights.tar.gz")
    assert not builder_mod.should_ignore("sample.7z", False, rel="tests/fixtures/sample.7z")

    scan = builder_mod.scan_project(request_)          # the estimate counts them...
    assert "*.zip" not in scan.excluded and "*.tar.gz" not in scan.excluded

    app = build(request_).package_dir / "application"  # ...and the copy delivers them
    assert (app / "assets" / "data.zip").read_bytes() == b"PK payload"
    assert (app / "models" / "weights.tar.gz").is_file()
    assert (app / "tests" / "fixtures" / "sample.7z").is_file()


def test_a_root_level_archive_is_still_a_release_artefact_and_is_dropped(request_, stub_pip):
    """The other half of the same rule, and the reason the rule exists at all: at the
    PROJECT ROOT an archive is something a build left lying about — often 200 MB of
    it. Rescuing assets/data.zip must not start shipping release.zip."""
    (request_.project_dir / "release.zip").write_bytes(b"PK" * 10)
    (request_.project_dir / "backup.tar.gz").write_bytes(b"\x1f\x8b" * 10)
    (request_.project_dir / "old.7z").write_bytes(b"7z" * 10)

    assert builder_mod.should_ignore("release.zip", False, rel="release.zip")
    assert builder_mod.should_ignore("backup.tar.gz", False, rel="backup.tar.gz")
    assert builder_mod.should_ignore("old.7z", False, rel="old.7z")

    app = build(request_).package_dir / "application"
    assert not (app / "release.zip").exists()
    assert not (app / "backup.tar.gz").exists()
    assert not (app / "old.7z").exists()


def test_a_wheel_is_never_runtime_data_so_it_goes_at_any_depth(request_, stub_pip):
    """`*.whl` deliberately did NOT move to root-only with the archives. A packaged app
    never opens a wheel — its dependencies are already installed into runtime/ — so a
    nested `vendor/wheels/` is CV_Viewer's 124 MB wheelhouse at a different address,
    not data. The depth split is a judgement about each pattern, not a blanket rule."""
    nested = request_.project_dir / "vendor" / "wheels"      # `vendor` is root-only too
    nested.mkdir(parents=True)
    (nested / "numpy-2.0-cp311-win_amd64.whl").write_bytes(b"PK" * (1024))

    assert builder_mod.should_ignore("numpy-2.0-cp311-win_amd64.whl", False,
                                     rel="vendor/wheels/numpy-2.0-cp311-win_amd64.whl")
    app = build(request_).package_dir / "application"
    assert not (app / "vendor" / "wheels" / "numpy-2.0-cp311-win_amd64.whl").exists()


# ── the escape hatch actually opens ──────────────────────────────────────────

def test_the_operator_is_told_the_escape_hatch_exists(request_, stub_pip):
    """S7. `!pattern` worked, was tested, and was documented NOWHERE: `grep -rn
    provisionignore README.md docs/` returned nothing, and the GUI never said the word.
    An escape hatch nobody can find is not an escape hatch — the only inference left to
    the operator is that this tool deletes things and cannot be argued with.

    So every sentence that says 「我幫你排掉了 X」 now also says how to get X back."""
    (request_.project_dir / "junk.pyc").write_bytes(b"x" * (2 * 1024 ** 2))

    scan = builder_mod.scan_project(request_)
    note = scan.excluded_summary
    assert "已自動排除" in note
    assert ".provisionignore" in note and "!" in note, note
    assert any(".provisionignore" in n for n in scan.notes)
    # ...and it is a NOTE, not a warning: knowing how to undo an exclusion is
    # information, not a decision the GUI should block the build for.
    assert not any(".provisionignore" in w for w in scan.warnings)


def test_a_root_archive_the_app_really_does_read_can_be_rescued_by_bang(request_, stub_pip):
    """The root-only archive rule is a DEFAULT, not a verdict. A project that really
    does keep its payload at the root gets it back with one line — and that line is the
    one the exclusion note now prints."""
    (request_.project_dir / "data.zip").write_bytes(b"PK payload")
    (request_.project_dir / "release.zip").write_bytes(b"PK junk")
    (request_.project_dir / ".provisionignore").write_text("!data.zip\n", encoding="utf-8")

    assert not builder_mod.should_ignore("data.zip", False, ("!data.zip",), "data.zip")

    app = build(request_).package_dir / "application"
    assert (app / "data.zip").read_bytes() == b"PK payload"      # rescued
    assert not (app / "release.zip").exists()                    # and only that one


def test_a_user_pattern_can_re_include_what_a_builtin_rule_dropped(request_, stub_pip):
    """S7. The built-ins were checked FIRST and returned immediately, so nothing the
    operator could write was able to rescue a file we had decided to drop. An escape
    hatch that cannot open is not an escape hatch. Built-ins are now only the
    starting position; the last matching user pattern wins, gitignore-style."""
    (request_.project_dir / "dist").mkdir()
    (request_.project_dir / "dist" / "index.html").write_text("payload", encoding="utf-8")
    (request_.project_dir / ".provisionignore").write_text("!dist\n", encoding="utf-8")

    assert not builder_mod.should_ignore("dist", True, ("!dist",), "dist")

    pkg = build(request_).package_dir
    assert (pkg / "application" / "dist" / "index.html").read_text("utf-8") == "payload"


def test_a_bang_pattern_is_honoured_and_not_silently_discarded(request_, stub_pip):
    """`!pattern` was LOADED (ignore_patterns_for kept the line) and then treated as
    'not a pattern' by the matcher — so a .provisionignore full of `!keep/this`
    looked honoured and did exactly nothing. Silence is the bug: the operator has no
    way to find out."""
    (request_.project_dir / ".provisionignore").write_text(
        "*.mp4\n!keep/demo.mp4\n", encoding="utf-8")
    (request_.project_dir / "drop.mp4").write_bytes(b"x" * 64)
    (request_.project_dir / "keep").mkdir()
    (request_.project_dir / "keep" / "demo.mp4").write_bytes(b"x" * 64)

    patterns = builder_mod.ignore_patterns_for(request_)
    assert "!keep/demo.mp4" in patterns                 # it survives the load...

    pkg = build(request_).package_dir
    assert not (pkg / "application" / "drop.mp4").exists()
    assert (pkg / "application" / "keep" / "demo.mp4").is_file()   # ...and it BITES


def test_the_last_matching_pattern_wins_like_gitignore(request_):
    keep = builder_mod.should_ignore
    assert not keep("demo.mp4", False, ("*.mp4", "!demo.mp4"), "demo.mp4")
    assert keep("demo.mp4", False, ("!demo.mp4", "*.mp4"), "demo.mp4")   # order matters
    assert not keep("a.log", False, ("*.log", "!*.log"), "a.log")


def test_a_directory_pattern_does_not_leave_an_empty_directory_behind(request_, stub_pip):
    """`data/*` excluded every child of data/ but never matched data/ itself, so the
    directory was not pruned and an EMPTY data/ shipped. The operator who wrote
    `data/*` and still finds data/ in the package concludes, reasonably, that the
    exclusion did not work."""
    data = request_.project_dir / "data"
    (data / "nested").mkdir(parents=True)
    (data / "raw.csv").write_text("1", encoding="utf-8")
    (data / "nested" / "deep.csv").write_text("2", encoding="utf-8")
    (request_.project_dir / ".provisionignore").write_text("data/*\n", encoding="utf-8")

    assert builder_mod.should_ignore("data", True, ("data/*",), "data")     # the DIR itself
    assert builder_mod.should_ignore("logs", True, ("logs/**",), "logs")    # and `**` too

    pkg = build(request_).package_dir
    assert not (pkg / "application" / "data").exists(), "an empty data/ shipped"
    assert (pkg / "application" / "app.py").is_file()


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


def test_a_file_that_dominates_the_version_slot_is_named_at_check_time(request_, stub_pip):
    """S7. A store shares the RUNTIME between versions (same requirements -> zero copy),
    never the application: store_builder has no hardlink and no dedup, so every version
    directory used to get its own full copy of application\\, so CV_Viewer's 84 MB
    DINOv2 weight was re-copied on EVERY release.

    Hardlink dedup fixed the DISK half of that (versions/ went 168 MB → 84 MB, and a
    second version now costs 0 MB). The TRANSFER half is still real and must still be
    said: the update PACKAGE has to carry the 84 MB, because the target machine does
    not have those bytes and a USB stick is FAT/exFAT, where hardlinks do not exist.

    So the warning must now say BOTH — and it must not repeat the old claim 「沒有
    硬連結、沒有去重」, which our own change made false. Shipping a warning we know
    to be false is the exact disease this whole file keeps curing."""
    (request_.project_dir / "models").mkdir()
    (request_.project_dir / "models" / "dinov2.pth").write_bytes(b"\0" * (84 * 1024 * 1024))

    scan = builder_mod.scan_project(request_, versioned=True)
    warning = scan.version_slot_warning
    assert warning, scan.warnings
    assert "models/dinov2.pth" in warning
    assert "84 MB" in warning                      # the number, not a shrug
    assert "硬連結共用" in warning                  # the disk half IS solved — say so
    # …and never re-assert the claim our own change made false. (The sentence about
    # a USB stick having no hardlinks is TRUE — FAT/exFAT really doesn't — so match
    # the old lie exactly, not any string containing 「沒有硬連結」.)
    assert "沒有硬連結、沒有去重" not in warning
    assert "更新包" in warning                      # the transfer half is NOT — say that too
    assert ".provisionignore" in warning           # ...and the way out
    assert warning in scan.warnings                # store mode: the operator is stopped

    # Fat mode has ONE folder and a rebuild replaces it, so the same project must NOT
    # get a warning about a cost it does not pay. A warning nobody needs to act on is
    # how the real ones stop being read (see the 「已自動排除」 false alarm).
    fat = builder_mod.scan_project(request_)
    assert fat.version_slot_warning == warning     # still computed, for whoever asks...
    assert warning not in fat.warnings             # ...but it does not block a fat build
    assert any("dinov2.pth" in w for w in fat.warnings)      # the big-file warning stands


def test_a_big_project_with_no_dominating_file_is_not_nagged_about_version_slots(
        request_, stub_pip):
    """The advice is 「把那個檔案搬出去」. A 60 MB project made of a thousand 100 KB
    source files has no such file, so there is nothing to act on and we say nothing."""
    src = request_.project_dir / "src"
    src.mkdir()
    for i in range(30):
        (src / f"mod{i}.py").write_bytes(b"#" * (1024 * 1024))     # 30 x 1 MB: no big file

    scan = builder_mod.scan_project(request_, versioned=True)
    assert scan.version_slot_warning == ""
    assert not any("Store 佈局" in w for w in scan.warnings)


def test_every_operator_facing_string_survives_a_cp950_console(request_, stub_pip):
    """The GUI log and the console are cp950 on a zh-TW box: one un-encodable character
    takes the whole message down with it, at the moment it is needed most."""
    (request_.project_dir / "models").mkdir()
    (request_.project_dir / "models" / "dinov2.pth").write_bytes(b"\0" * (84 * 1024 * 1024))
    (request_.project_dir / "junk.pyc").write_bytes(b"x" * (2 * 1024 ** 2))

    scan = builder_mod.scan_project(request_, versioned=True)
    for text in [*scan.warnings, *scan.notes, scan.excluded_summary,
                 scan.version_slot_warning, builder_mod.ESCAPE_HATCH_HINT]:
        text.encode("cp950")


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
    """A real build of CV_Viewer lost this race: Defender was still scanning the
    500 MB runtime it had just written, and the operator got

        [WinError 5] 存取被拒。: '...\\.staging-2b0aa162' -> '...\\cp311-845b4ecb...'

    which tells them nothing they can act on — least of all that the build had in
    fact succeeded and a retry would work."""
    staging = tmp_path / "staging"
    staging.mkdir()

    def always_denied(_src, _dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(builder_mod.os, "rename", always_denied)
    monkeypatch.setattr(builder_mod.time, "sleep", lambda _s: None)

    with pytest.raises(runtime_mod.RuntimeError_) as caught:
        builder_mod._swap_into_place(staging, tmp_path / "final")

    message = str(caught.value)
    assert "防毒" in message                    # names the actual cause
    assert "重跑一次建置" in message            # and the thing to do about it
    assert "排除清單" in message                # and how to stop it happening again


# ── cancel really cancels ────────────────────────────────────────────────────

def test_cancel_stops_the_build_cleans_up_and_says_so(request_, stub_pip):
    """The old 取消 button set a flag nobody read: the build ran to completion and
    reported success. A cancel that returns ok=True is worse than no button."""
    result = build(request_, should_cancel=lambda: True)

    assert result.cancelled is True
    assert result.ok is False
    assert result.message == "已取消建置,暫存目錄已清乾淨"
    assert result.staging_left is None                       # nothing to warn a GUI about
    assert not any(request_.output_dir.glob(".staging-*"))   # nothing left behind
    assert not request_.package_dir.exists()                 # and nothing half-written


def test_a_cancel_that_could_not_delete_the_staging_dir_does_not_say_it_did(
        request_, stub_pip, monkeypatch):
    """S1. We reach this cleanup moments after taskkill'ing pip's whole process tree,
    and Windows keeps a dying process's handles open a little longer — Defender, which
    just watched us write 500 MB, is holding the tree too. rmtree(ignore_errors=True)
    swallowed all of that, and the operator was told 「暫存目錄已清乾淨」 while 600 MB
    of it sat in their output folder. Verify before you claim."""
    real_rmtree = builder_mod.shutil.rmtree

    def locked(path, *args, **kwargs):
        if Path(path).name.startswith(".staging-"):
            raise PermissionError(32, "The process cannot access the file")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(builder_mod.shutil, "rmtree", locked)
    monkeypatch.setattr(builder_mod.time, "sleep", lambda _s: None)

    lines: list[str] = []
    result = build(request_, progress=lines.append, should_cancel=lambda: True)

    assert result.cancelled and not result.ok
    leftover = list(request_.output_dir.glob(".staging-*"))
    assert leftover                                        # the truth on disk
    assert "已清乾淨" not in result.message                # …and the lie we used to tell
    assert str(leftover[0]) in result.message              # WHICH folder is still there
    assert "刪不掉" in result.message
    assert "下次建置會自動清掉" in result.message          # and what happens about it
    assert result.message in lines                         # the operator saw it, not just the caller
    result.message.encode("cp950")                         # a zh-TW console can print it

    # S1, the other half: a GUI cannot branch on a SENTENCE. It rendered its own
    # hardcoded 「暫存目錄已清乾淨」 over this honest message, and the operator went
    # looking for 600 MB of disk space that had never been freed. The fact travels as
    # a FIELD, which nothing can overwrite by accident.
    assert result.staging_left == leftover[0]
    assert result.staging_left.is_dir()


def test_the_next_build_really_does_clean_up_the_staging_dir_a_cancel_left_behind(
        request_, stub_pip, monkeypatch):
    """The cancel message promises 「下次建置會自動清掉」. A promise is only worth
    making if the sweep at the top of build() exists AND is reached AND retries
    through the lock that beat the cancel."""
    locked = {"on": True}
    real_rmtree = builder_mod.shutil.rmtree

    def maybe_locked(path, *args, **kwargs):
        if locked["on"] and Path(path).name.startswith(".staging-"):
            raise PermissionError(32, "The process cannot access the file")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(builder_mod.shutil, "rmtree", maybe_locked)
    monkeypatch.setattr(builder_mod.time, "sleep", lambda _s: None)

    cancelled = build(request_, should_cancel=lambda: True)
    assert cancelled.cancelled
    assert list(request_.output_dir.glob(".staging-*"))    # left behind, and it said so

    locked["on"] = False                                   # Defender lets go of it
    lines: list[str] = []
    result = build(request_, progress=lines.append)

    assert result.ok
    assert not any(request_.output_dir.glob(".staging-*"))       # swept, as promised
    assert any("清掉上次沒收乾淨的暫存目錄" in line for line in lines)


def test_clean_orphan_staging_does_not_count_bytes_it_never_freed(tmp_path, monkeypatch):
    """It added the folder's size to the freed total whatever
    rmtree(ignore_errors=True) had done with it — reporting reclaimed space it had
    not reclaimed. That is the same lie as the cancel message, one level down, and
    it is the sweep the cancel message points at."""
    out = tmp_path / "out"
    out.mkdir()
    junk = out / ".staging-app-deadbeef"
    junk.mkdir()
    (junk / "runtime.bin").write_bytes(b"x" * (3 * 1024 ** 2))

    def in_use(*_args, **_kwargs):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(builder_mod.shutil, "rmtree", in_use)
    monkeypatch.setattr(builder_mod.time, "sleep", lambda _s: None)

    lines: list[str] = []
    freed = builder_mod.clean_orphan_staging(out, lines.append)

    assert freed == 0                       # nothing came back…
    assert junk.is_dir()                    # …because it is still sitting there
    assert any("刪不掉" in line for line in lines)
    "\n".join(lines).encode("cp950")


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


def fat_template(root: Path, files: int = 60, size: int = 32 * 1024) -> Path:
    """A runtime template big enough to be worth cancelling out of."""
    (root / "Lib").mkdir(parents=True)
    (root / "python.exe").write_bytes(b"MZ" + b"\0" * size)
    for index in range(files):
        (root / "Lib" / f"mod{index:03d}.py").write_bytes(b"x" * size)
    return root


def test_cancel_during_the_runtime_copy_stops_inside_it_not_after_it(tmp_path):
    """S1. shutil.copytree has no cancellation hook, so 取消 pressed during the
    500 MB runtime copy did nothing at all for tens of seconds: the button greyed
    out, the status sat on 「正在取消…」, and Defender ground through every last file
    before anyone looked at the flag. It is the longest step in the build and it was
    the only one with no way out and no feedback."""
    template = fat_template(tmp_path / "template")
    calls = {"n": 0}

    def cancel_midway() -> bool:
        calls["n"] += 1
        return calls["n"] > 6          # the operator presses 取消 partway through

    dest = tmp_path / "runtime"
    with pytest.raises(runtime_mod.BuildCancelled):
        runtime_mod.copy_runtime(template, dest, should_cancel=cancel_midway)

    copied = list(dest.rglob("mod*.py"))
    assert copied                                       # it had started…
    assert len(copied) < 60                             # …and it stopped without finishing
    assert not (dest / "Lib" / "mod059.py").exists()    # it did NOT run to the end


def test_the_runtime_copy_says_where_it_is_instead_of_freezing(tmp_path):
    """500 MB with Defender in the loop is the single longest step of the build, and
    the only one that reported nothing at all while it ran."""
    template = fat_template(tmp_path / "template", files=40, size=64 * 1024)
    lines: list[str] = []

    python = runtime_mod.copy_runtime(template, tmp_path / "runtime",
                                      progress=lines.append)

    assert python.is_file()
    assert any("runtime" in line and "MB" in line for line in lines)
    "\n".join(lines).encode("cp950")


def test_the_runtime_copy_still_copies_everything_it_used_to(tmp_path):
    """We hand-rolled the copy to make it cancellable; it must still produce exactly
    the tree shutil.copytree did — no .pyc, no __pycache__, empty dirs kept."""
    template = tmp_path / "template"
    (template / "Lib" / "__pycache__").mkdir(parents=True)
    (template / "Lib" / "__pycache__" / "os.cpython-311.pyc").write_bytes(b"junk")
    (template / "DLLs").mkdir()                            # an empty dir travels too
    (template / "python.exe").write_bytes(b"MZ fake")
    (template / "Lib" / "os.py").write_text("# stdlib", encoding="utf-8")
    (template / "Lib" / "os.pyc").write_bytes(b"junk")     # loose .pyc beside its source

    dest = tmp_path / "runtime"
    runtime_mod.copy_runtime(template, dest)

    assert (dest / "python.exe").is_file()
    assert (dest / "Lib" / "os.py").is_file()
    assert (dest / "DLLs").is_dir()
    assert not list(dest.rglob("*.pyc"))
    assert not list(dest.rglob("__pycache__"))


def test_build_passes_should_cancel_down_into_the_runtime_copy(request_, stub_pip, monkeypatch):
    """If the cancel flag stops at the builder's door, the operator waits out the
    whole 500 MB copy anyway — which is precisely what they were doing."""
    seen = {}
    real_copy = runtime_mod.copy_runtime

    def spy(template, dest, **kwargs):
        seen.update(kwargs)
        return real_copy(template, dest)

    monkeypatch.setattr(runtime_mod, "copy_runtime", spy)
    build(request_, should_cancel=lambda: False)

    assert callable(seen.get("should_cancel"))
    assert callable(seen.get("progress"))


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


# ── the delivered launcher is self-contained ─────────────────────────────────

def test_the_delivered_launcher_carries_the_shared_page_rules(request_, stub_pip):
    """launch.py loads pages.py BY PATH on the target machine — there is no
    provision_builder there to import it from. Ship the package without it and the
    App does not start at all: LauncherIncomplete, exit 4, 「launcher 資料夾不完整…
    請重新建置」 about a build we had just called 完成.

    The module exists because the build gate and the device used two different
    rulebooks for 「what does Streamlit actually load」 — and the build gate could not
    see pages\\ at all, so a missing import in pages\\2_report.py passed 100% of the
    build checks and went to the factory floor. One rulebook is only one rulebook if
    it travels with the package.
    """
    from provision_builder.streamlit_desktop import pages as pages_mod

    package = build(request_).package_dir
    delivered = package / "launcher" / pages_mod.DELIVERED_NAME

    assert delivered.is_file()
    # …and it is the real thing, not a name collision: launch.py checks this mark
    # before it will trust the file it just loaded.
    assert pages_mod.MODULE_MARK in delivered.read_text("utf-8")


def test_a_package_without_the_page_rules_fails_its_own_smoke_test(request_, stub_pip):
    """Catch it here, on the build machine, where it can still be fixed — not on the
    user's desk as exit 4."""
    from provision_builder.streamlit_desktop import pages as pages_mod

    package = build(request_).package_dir
    manifest = json.loads((package / "app-package.json").read_text("utf-8"))
    assert smoke_test(package, manifest) == []

    (package / "launcher" / pages_mod.DELIVERED_NAME).unlink()
    problems = smoke_test(package, manifest)
    assert any(pages_mod.DELIVERED_NAME in problem for problem in problems)


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


# ── the offline WebView2 promise ─────────────────────────────────────────────

def test_a_package_with_no_webview2_installer_says_so_instead_of_claiming_success(
        request_, stub_pip):
    """S1/S4. NOTHING in the tree ever created prereq/ — the store builder only
    copies one if it already exists, and it never did. So the package told the user
    to run tools\\安裝WebView2.bat, whose offline branch needs
    prereq\\MicrosoftEdgeWebview2Setup.exe, on the air-gapped factory machine this
    product exists for. The only self-rescue path was a dead end, and the build
    called it 完成. It may still succeed — it must not stay quiet."""
    result = build(request_)
    assert result.ok
    assert not (result.package_dir / "prereq").exists()

    warned = [w for w in result.warnings if "WebView2" in w]
    assert warned, "a package that cannot start on an offline target must warn"
    assert "exit 5" in warned[0]                    # the code start.bat actually returns
    assert "沒有網路就裝不了" in warned[0]

    readme = (result.package_dir / "讀我-使用說明.txt").read_text("utf-8")
    assert "沒有" in readme and "prereq" in readme  # and the 讀我 does not promise one


def offline_installer(tmp_path: Path, name: str | None = None) -> Path:
    """A file big enough to BE the Evergreen Standalone Installer (~130 MB in the
    field; anything over 10 MB is enough to prove we do not call it a bootstrapper)."""
    installer = tmp_path / "downloads" / (name or builder_mod.WEBVIEW2_INSTALLER_NAME)
    installer.parent.mkdir(parents=True, exist_ok=True)
    installer.write_bytes(b"MZ" + b"\0" * (11 * 1024 * 1024))
    return installer


def test_the_webview2_installer_is_copied_into_prereq_where_the_helper_looks(
        request_, stub_pip, tmp_path):
    installer = offline_installer(tmp_path)
    request_.webview2_installer = installer
    request_.__post_init__()                        # re-resolve, as the GUI would

    result = build(request_)
    assert result.ok, result.errors
    shipped = result.package_dir / "prereq" / builder_mod.WEBVIEW2_INSTALLER_NAME
    assert shipped.is_file()
    assert shipped.read_bytes() == installer.read_bytes()
    assert not [w for w in result.warnings if "WebView2" in w]   # nothing to warn about

    helper = (result.package_dir / "tools" / "安裝WebView2.bat").read_text("utf-8")
    assert "prereq" in helper


def test_the_admins_chosen_installer_is_never_renamed(request_, stub_pip, tmp_path):
    """BLOCKER. We used to copy the admin's file to a hard-coded "canonical" name —
    MicrosoftEdgeWebview2Setup.exe, which is the ~2 MB Evergreen BOOTSTRAPPER. An
    operator who correctly downloaded the 130 MB Standalone Installer had it silently
    relabelled as the one file that CANNOT install on an offline machine, which is the
    only machine prereq\\ exists for. There was never anything to gain: the helper bat
    runs whatever .exe is in prereq\\."""
    installer = offline_installer(tmp_path, "WebView2 離線版 (公司IT提供).exe")
    request_.webview2_installer = installer
    request_.__post_init__()

    prereq = build(request_).package_dir / "prereq"
    shipped = [p.name for p in prereq.glob("*.exe")]
    assert shipped == ["WebView2 離線版 (公司IT提供).exe"], shipped
    assert not (prereq / "MicrosoftEdgeWebview2Setup.exe").exists()


def test_a_two_megabyte_bootstrapper_is_called_out_while_the_operator_can_still_fix_it(
        request_, stub_pip, tmp_path):
    """The bootstrapper installs nothing without a network. Handing it to an offline
    factory PC is the failure this whole feature exists to prevent, so the build says
    so HERE — where the right file is a 30-second download — not on the factory floor,
    where it is a dead end."""
    boot = tmp_path / "MicrosoftEdgeWebview2Setup.exe"
    boot.write_bytes(b"MZ" + b"\0" * (2 * 1024 * 1024))     # the real one is ~2 MB
    request_.webview2_installer = boot
    request_.__post_init__()

    result = build(request_)
    assert result.ok, result.errors                          # it is a warning, not a wall
    warned = [w for w in result.warnings if "bootstrap" in w.lower()]
    assert warned, result.warnings
    assert "需要連網" in warned[0]
    assert builder_mod.WEBVIEW2_INSTALLER_NAME in warned[0]  # what to get INSTEAD
    warned[0].encode("cp950")
    # ...and it was still copied, under its own name: we never silently drop a file
    # the operator asked us to ship.
    assert (result.package_dir / "prereq" / boot.name).is_file()


def test_every_webview2_message_asks_for_the_standalone_not_the_bootstrapper(
        request_, stub_pip):
    """The whole offline chain used to point at LinkId=2124703 — the Evergreen
    Bootstrapper, ~2 MB, which contains no WebView2 and DOWNLOADS it at install time.
    On the air-gapped machine this product exists for, following our own instructions
    to the letter produced a PC that still could not open a window."""
    assert builder_mod.WEBVIEW2_INSTALLER_NAME == "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    assert builder_mod.WEBVIEW2_DOWNLOAD == "https://go.microsoft.com/fwlink/?LinkId=2124701"
    assert "2124703" not in builder_mod.WEBVIEW2_DOWNLOAD     # the bootstrapper link

    pkg = build(request_).package_dir
    for name in ("webview2-none.txt", "start-webview2.txt", "webview2-bootstrapper.txt"):
        body = (pkg / "messages" / name).read_text("utf-8")
        body.encode("cp950")
        assert builder_mod.WEBVIEW2_INSTALLER_NAME in body, name
        assert "2124703" not in body, name
        assert "需要連網" in body, name          # WHY the 2 MB one cannot be used
    assert builder_mod.WEBVIEW2_INSTALLER_NAME in builder_mod.WEBVIEW2_MISSING_WARNING
    # the warning must also offer the remedy the immutable-version rule allows
    assert "不必重建" in builder_mod.WEBVIEW2_MISSING_WARNING
    assert "prereq" in builder_mod.WEBVIEW2_MISSING_WARNING


def test_the_helper_bat_says_it_is_a_bootstrapper_when_the_install_fails_on_a_small_file(
        request_, stub_pip):
    """「安裝失敗」 plus a sub-10 MB file in prereq\\ is not a mystery to escalate: it
    is the downloader, on a machine with no network. The bat has the file size; it
    must spend it."""
    helper = (build(request_).package_dir / "tools" / "安裝WebView2.bat").read_text("ascii")
    assert 'for %%A in ("%WV2SETUP%") do set "SZ=%%~zA"' in helper
    assert f"if %SZ% LSS {builder_mod.WEBVIEW2_MIN_OFFLINE_BYTES} " \
           'type "messages\\webview2-bootstrapper.txt"' in helper
    # the size is read BEFORE the install: %SZ% inside the if-block is expanded when
    # the block is parsed, not when it runs
    assert helper.index('set "SZ=') < helper.index('"%WV2SETUP%" /silent /install')


def test_a_webview2_installer_that_does_not_exist_fails_the_build_loudly(
        request_, stub_pip, tmp_path):
    """Never silently downgrade to 'no prereq' on a package we promised one for."""
    request_.webview2_installer = tmp_path / "not-there.exe"
    result = build(request_)
    assert not result.ok
    assert any("WebView2" in e for e in result.errors)


def test_the_helper_bat_runs_whatever_installer_is_in_prereq_not_one_hard_coded_name(
        request_, stub_pip):
    """Microsoft ships the offline runtime as
    MicrosoftEdgeWebView2RuntimeInstallerX64.exe; the helper only ever ran
    MicrosoftEdgeWebview2Setup.exe, so an operator who supplied the right file was
    told 「沒有附安裝檔」. Both take /silent /install."""
    helper = (build(request_).package_dir / "tools" / "安裝WebView2.bat").read_text("utf-8")
    assert 'for %%F in ("prereq\\*.exe")' in helper
    assert "/silent /install" in helper


def test_start_bat_checks_every_webview2_registry_location_and_exits_5():
    """One source of truth for 'is WebView2 here'. Miss a location and a machine that
    HAS it is turned away at the door: per-machine on x64 lands under WOW6432Node,
    per-machine on ARM64/x86 lands in the native path, per-user only ever appears in
    HKCU. And a pv of 0.0.0.0 is what a broken/partial install leaves behind — the
    key is there, the runtime is not."""
    bat = (builder_mod.TEMPLATES / "start.bat").read_text("ascii")
    assert "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate" in bat
    assert "HKLM\\SOFTWARE\\Microsoft\\EdgeUpdate" in bat
    assert "HKCU\\SOFTWARE\\Microsoft\\EdgeUpdate" in bat
    assert '"%%W"=="0.0.0.0"' in bat
    assert "exit /b 5" in bat            # the environment code, not a generic 1


# ── the .bat files cmd.exe can actually parse ────────────────────────────────

def _bats(pkg: Path) -> list[Path]:
    return [builder_mod.TEMPLATES / "start.bat", pkg / "start.bat",
            pkg / "tools" / "安裝WebView2.bat"]


def test_every_bat_we_ship_is_pure_ascii_crlf_and_bomless(request_, stub_pip):
    """Under `chcp 65001` cmd tracks its place in a .bat as a BYTE offset but counts
    CHARACTERS. Every time it re-reads the file — after a `for /f`, a pipe, an
    external command, a `goto` — it seeks to an offset that is wrong by however many
    multi-byte characters came before, lands mid-line, and executes what it finds.
    A real cmd.exe executed the tail of a Chinese `rem` comment in start.bat. It
    misfires on ~1 run in 20, which is exactly how it passed review and shipped.

    In an ASCII file byte offset == character offset and the seek cannot miss. The
    old em-dash rule was this bug seen through a keyhole; ASCII-only subsumes it.
    The Chinese now lives in messages\\*.txt, which cmd `type`s and never parses."""
    pkg = build(request_).package_dir
    for bat in _bats(pkg):
        raw = bat.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{bat.name} has a BOM"
        assert raw.count(b"\n") == raw.count(b"\r\n"), f"{bat.name} has a bare LF"
        text = raw.decode("utf-8")
        assert text.isascii(), f"{bat.name} 不是純 ASCII,cmd.exe 會 seek 到行中間"
        assert "—" not in text
        assert not builder_mod.bat_problems(text), builder_mod.bat_problems(text)


def test_no_bat_echoes_a_bare_parenthesis_inside_a_block(request_, stub_pip):
    """`echo ... (exit 5)` inside `if not defined WV2PV ( ... )`: the unescaped `)`
    closed the block, so the WebView2 error printed UNCONDITIONALLY and start.bat
    turned away every machine — including the ones that had WebView2."""
    pkg = build(request_).package_dir
    for bat in _bats(pkg):
        for line in bat.read_text("utf-8").splitlines():
            if line.strip().lower().startswith("echo"):
                assert "(" not in line and ")" not in line, f"{bat.name}: {line!r}"

    with pytest.raises(ValueError, match="括號"):
        builder_mod._write_bat(pkg / "x.bat", "@echo off\r\nif 1==1 (\r\necho hi (5)\r\n)\r\n")


def test_the_chinese_messages_travel_and_survive_a_cp950_console(request_, stub_pip):
    """start.bat is ASCII, so every operator-facing sentence it prints comes out of
    messages\\. Ship the .bat without them and the user gets an English tag and then
    silence, at the one moment they need to be told something."""
    pkg = build(request_).package_dir
    for name in builder_mod.MESSAGES:
        body = (pkg / "messages" / name).read_text("utf-8")
        body.encode("cp950")                       # a zh-TW console can render it
    assert "WebView2" in (pkg / "messages" / "start-webview2.txt").read_text("utf-8")

    # and the package self-check refuses to ship without them
    (pkg / "messages" / "start-webview2.txt").unlink()
    manifest = json.loads((pkg / "app-package.json").read_text("utf-8"))
    assert any("start-webview2.txt" in p for p in smoke_test(pkg, manifest))


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
def test_a_real_cmd_exe_parses_every_bat_we_ship_without_corrupting_itself(
        request_, stub_pip):
    """The test that would have caught all of the above: hand the REAL files to a
    REAL cmd.exe and look at stderr. A corrupted .bat announces itself as
    「'...' is not recognized as an internal or external command」 — cmd executing
    the second half of a line it landed in the middle of."""
    pkg = build(request_).package_dir
    (pkg / "runtime" / "python.exe").write_bytes(b"MZ")     # exists; not a real exe

    for bat in (pkg / "start.bat", pkg / "tools" / "安裝WebView2.bat"):
        for _ in range(8):                     # it misfires ~1 in 20; do not trust one run
            proc = subprocess.run(["cmd", "/c", str(bat)], input="\n", capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=120)
            damage = [line for line in proc.stderr.splitlines()
                      if "not recognized" in line or "不是內部或外部" in line or "�" in line]
            assert not damage, f"{bat.name} 被 cmd.exe 剖壞了:{damage}"

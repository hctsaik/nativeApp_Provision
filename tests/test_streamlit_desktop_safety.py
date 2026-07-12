"""The defects a scenario review found — each one shipped a broken package or a
lie to the operator. These tests exist so they cannot come back.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import imports as imports_mod
from provision_builder.streamlit_desktop import requirements as req_mod

TEMPLATES = (Path(__file__).resolve().parents[1] / "src" / "provision_builder"
             / "streamlit_desktop" / "templates")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_tmpl_{name}", TEMPLATES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launch = _load("launch")


# ── a read-only check must not write into the user's project ─────────────────

def test_checking_a_project_does_not_litter_in_it(tmp_path):
    """`resolve()` used to write requirements.from-pyproject.txt into the user's
    repository every time they pressed 「檢查專案」 — it really did appear in AI4BI."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["streamlit>=1.35"]\n', encoding="utf-8")

    found = req_mod.resolve(tmp_path)                      # no staging = read-only
    assert found.generated and found.path.name == "pyproject.toml"
    assert not (tmp_path / "requirements.from-pyproject.txt").exists()
    assert req_mod.declares_streamlit(found.path)          # still answerable

    staged = req_mod.resolve(tmp_path, staging=tmp_path / "staging")
    assert staged.path.parent == tmp_path / "staging"      # writes only when asked


# ── a missing dependency must be caught on the build machine ─────────────────

def test_imports_the_app_makes_are_checked_against_the_runtime(tmp_path, monkeypatch):
    """Streamlit answers /_stcore/health with 200 even when the script dies on
    `import missing_module` — so a forgotten dependency passes every health check
    and gets committed as last-known-good. Catch it at build time instead."""
    project = tmp_path / "proj"
    (project / "pages").mkdir(parents=True)
    (project / "app.py").write_text(
        "import streamlit as st\nimport cv2\nfrom helpers import util\n"
        "from pages.one import render\n", encoding="utf-8")
    (project / "helpers.py").write_text("def util(): pass\n", encoding="utf-8")
    (project / "pages" / "__init__.py").write_text("", encoding="utf-8")
    (project / "pages" / "one.py").write_text(
        "import nonexistent_pkg\ndef render(): pass\n", encoding="utf-8")

    names = imports_mod.top_level_imports(project, project / "app.py")
    # Reached transitively: app.py → pages.one → nonexistent_pkg. `helpers` is the
    # project's own module and is never a dependency.
    assert {"streamlit", "cv2", "nonexistent_pkg"} <= names
    assert "helpers" not in names
    assert "helpers" in imports_mod.local_module_names(project)   # its own module

    monkeypatch.setattr(imports_mod, "importable_in", lambda _py, wanted: {"streamlit"})
    missing = imports_mod.missing_dependencies(project / "app.py", project,
                                               tmp_path / "python.exe")
    assert "cv2" in missing.required
    assert "nonexistent_pkg" in missing.required
    assert "helpers" not in missing.required              # local, not a dependency
    assert "streamlit" not in missing.required            # installed


def test_only_code_the_app_can_actually_reach_is_scanned(tmp_path):
    """CV_Viewer imports playwright in verify/, experiments in spike/, tests in
    conftest.py; AI4BI has a playwright helper inside its own package. None of
    them are runtime dependencies — and no folder-name blacklist would have
    caught all three. Reachability from the entry script does."""
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "spike").mkdir()
    (project / "verify").mkdir()
    (project / "app.py").write_text("import streamlit as st\nimport helpers\n", encoding="utf-8")
    (project / "helpers.py").write_text("import pandas\n", encoding="utf-8")
    (project / "tests" / "test_e2e.py").write_text("import playwright\n", encoding="utf-8")
    (project / "conftest.py").write_text("import pytest\n", encoding="utf-8")
    (project / "spike" / "try.py").write_text("import some_experiment\n", encoding="utf-8")
    (project / "verify" / "repro.py").write_text("import playwright\n", encoding="utf-8")

    names = imports_mod.top_level_imports(project, project / "app.py")
    assert {"streamlit", "pandas"} <= names          # reached through helpers.py
    assert not {"playwright", "pytest", "some_experiment"} & names


def test_optional_imports_guarded_by_try_except_do_not_fail_the_build(tmp_path):
    """CV_Viewer degrades gracefully when cv2 is absent (HAS_CV2 = False)."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text(
        "import streamlit as st\n"
        "try:\n    import cv2\n    HAS_CV2 = True\n"
        "except ImportError:\n    HAS_CV2 = False\n", encoding="utf-8")

    names = imports_mod.top_level_imports(project, project / "app.py")
    assert "streamlit" in names
    assert "cv2" not in names          # optional by construction


def test_stdlib_imports_are_not_reported_as_missing(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text(
        "import json, pathlib, streamlit\n", encoding="utf-8")
    monkeypatch.setattr(imports_mod, "importable_in", lambda _py, wanted: {"streamlit"})
    report = imports_mod.missing_dependencies(project / "app.py", project,
                                              tmp_path / "python.exe")
    assert not report.required and not report.optional


# ── the false positive that made real projects unbuildable ───────────────────

def test_a_lazy_import_inside_a_function_is_optional_not_required(tmp_path):
    """AI4BI's `_call_anthropic()` does `import anthropic` INSIDE the method, on
    purpose, so that mock-mode never needs the SDK. The old scanner used
    ast.walk(), called it a hard requirement, and hard-failed the build — after a
    six-minute pip install. An import that only runs when a function is called
    cannot break the first render."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text(
        "import streamlit as st\n"
        "import pandas\n"
        "\n"
        "def call_llm(prompt):\n"
        "    import anthropic          # lazy on purpose\n"
        "    return anthropic.Anthropic()\n"
        "\n"
        "class Late:\n"
        "    import boto3\n",
        encoding="utf-8")

    required, optional = imports_mod.classify(project, project / "app.py")
    assert set(required) == {"streamlit", "pandas"}
    assert set(optional) == {"anthropic", "boto3"}

    report = imports_mod.missing_from_lock(project / "app.py", project,
                                           "streamlit==1.40.0\npandas==2.0.0\n")
    assert report.required == []                     # the build may proceed
    assert report.optional == ["anthropic", "boto3"]  # and the operator is told


def test_a_module_level_import_in_an_if_or_try_block_is_still_required(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text(
        "import sys\n"
        "import streamlit\n"
        "if sys.platform == 'win32':\n"
        "    import pywintypes\n"
        "try:\n"
        "    import duckdb\n"
        "finally:\n"
        "    pass\n",
        encoding="utf-8")

    required, _optional = imports_mod.classify(project, project / "app.py")
    assert {"streamlit", "pywintypes", "duckdb"} == set(required)


def test_missing_from_lock_names_the_module_and_where_it_is_imported(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("import streamlit\nimport duckdb\n", encoding="utf-8")

    report = imports_mod.missing_from_lock(project / "app.py", project, "streamlit==1.40.0\n")
    assert report.required == ["duckdb"]

    message = report.failure_message()
    assert "duckdb" in message
    assert "app.py:2" in message                       # WHERE it is imported
    assert "加進 requirements" in message               # way out #1
    assert "選用相依,請忽略" in message                 # way out #2
    assert "一定跑不起來" not in message                 # not an assertion


def test_import_aliases_map_to_their_distribution_names(tmp_path):
    """cv2 is not a package name; opencv-python is. Demanding `cv2==...` in the
    lock, or reporting cv2 as missing when opencv-python-headless IS declared,
    are the same bug seen from two sides."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text(
        "import streamlit\nimport cv2\nimport yaml\nfrom PIL import Image\n"
        "import sklearn\nfrom dateutil import tz\n", encoding="utf-8")

    lock = ("streamlit==1.40.0\nopencv-python-headless==4.10.0.84\nPyYAML==6.0.2\n"
            "pillow==11.0.0\nscikit-learn==1.5.2\npython-dateutil==2.9.0\n")
    report = imports_mod.missing_from_lock(project / "app.py", project, lock)
    assert report.required == [] and report.optional == []


def test_a_probe_that_cannot_run_must_not_condemn_every_module(tmp_path, monkeypatch):
    """`importable_in()` returned set() when the subprocess failed — "nothing is
    importable", i.e. "everything is missing". A tooling failure of ours must not
    be reported as the project's fault."""
    class Failed:
        returncode = 9009            # Windows: command not found
        stdout = ""
        stderr = "is not recognized as an internal or external command"

    monkeypatch.setattr(imports_mod.subprocess, "run", lambda *_a, **_k: Failed())
    with pytest.raises(imports_mod.ImportProbeError):
        imports_mod.importable_in(tmp_path / "python.exe", {"streamlit"})


def test_preflight_finds_first_party_modules_next_to_a_nested_entrypoint(tmp_path):
    """CV_Viewer's entrypoint is application/5_PG_Develop/app.py, and its 23 sibling
    modules live beside it — `streamlit run` puts the script's OWN directory on
    sys.path, so they import fine. A preflight that only looked in application/
    declared all 23 of them missing PyPI packages, exited 3, and told the admin to
    add `casepkg` to requirements — a package that does not exist on PyPI.
    The delivered folder could not start at all while the build said 建立完成."""
    app_root = tmp_path / "application"
    nested = app_root / "5_PG_Develop"
    nested.mkdir(parents=True)
    (nested / "casepkg.py").write_text("import json\n", encoding="utf-8")
    (nested / "viewer.py").write_text("import casepkg\n", encoding="utf-8")
    (app_root / "shared.py").write_text("import os\n", encoding="utf-8")
    (nested / "app.py").write_text(
        "import streamlit as st\nimport casepkg\nimport viewer\nimport shared\n"
        "st.title('x')\n", encoding="utf-8")

    missing, syntax_error = launch.preflight(nested / "app.py", app_root)
    assert syntax_error is None
    assert missing == []          # not one of them is a third-party package


def test_preflight_still_catches_a_genuinely_missing_package(tmp_path):
    """The looser module search must not turn the gate off."""
    app_root = tmp_path / "application"
    (app_root / "pages").mkdir(parents=True)
    (app_root / "pages" / "app.py").write_text(
        "import streamlit as st\nimport definitely_not_installed_pkg\nst.title('x')\n",
        encoding="utf-8")

    missing, _ = launch.preflight(app_root / "pages" / "app.py", app_root)
    assert missing == ["definitely_not_installed_pkg"]
    # and the message still names the distribution for the aliased ones
    assert "opencv-python" in launch.missing_modules_message(["cv2"], app_root)


# ── "this version works" must mean the window really opened ──────────────────

class FakeProc:
    def __init__(self, alive=True, code=0):
        self.pid = 99
        self.returncode = None if alive else code
        self._code = code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code


def test_healthy_marker_is_not_written_when_the_shell_dies_instantly(tmp_path, monkeypatch):
    """A machine without the WebView2 runtime kills the shell in about a second.
    Writing the marker before that (as we used to) commits a version that cannot
    show a window as 'last known good' — poisoning the very version we roll back to."""
    marker = tmp_path / "healthy"
    monkeypatch.setenv("CIM_HEALTHY_MARKER", str(marker))
    monkeypatch.setattr(launch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *_a, **_k: FakeProc(alive=False, code=1))

    manifest = {"_shell": tmp_path / "cim-light.exe", "_shim": tmp_path / "shim.py",
                "_python": tmp_path / "python.exe", "app_id": "app-x", "display_name": "X"}

    class FakeControl:
        url = "http://127.0.0.1:1"
        token = "t"

    written = []
    code = launch.run_shell(manifest, FakeControl(), tmp_path,
                            on_window_ready=lambda: written.append(True))
    assert code != 0
    assert not written and not marker.exists()


def test_healthy_marker_is_written_once_the_window_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(launch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(launch, "SHELL_ALIVE_SECONDS", 0)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *_a, **_k: FakeProc(alive=True))

    manifest = {"_shell": tmp_path / "cim-light.exe", "_shim": tmp_path / "shim.py",
                "_python": tmp_path / "python.exe", "app_id": "app-x", "display_name": "X"}

    class FakeControl:
        url = "http://127.0.0.1:1"
        token = "t"

    written = []
    launch.run_shell(manifest, FakeControl(), tmp_path,
                     on_window_ready=lambda: written.append(True))
    assert written == [True]


def _supervisor_with_log(tmp_path: Path, text: str):
    log = tmp_path / "streamlit.log"
    log.write_text(text, encoding="utf-8")
    supervisor = launch.StreamlitSupervisor(
        {"_python": tmp_path / "python.exe", "_entrypoint": tmp_path / "app.py",
         "host": "127.0.0.1", "preferred_port": 0}, tmp_path)
    supervisor._proc = FakeProc(alive=True)
    supervisor._port = 9999
    supervisor._log_path = log
    return supervisor


def test_an_app_that_raises_on_import_is_not_called_healthy(tmp_path):
    """/_stcore/health is answered by the SERVER, not the app script: an app that
    dies on `import cv2` still gets a cheerful 200. The log is the only witness."""
    supervisor = _supervisor_with_log(
        tmp_path,
        "2026-01-01 Uvicorn running\n"
        "Traceback (most recent call last):\n"
        "ModuleNotFoundError: No module named 'cv2'\n")

    error = supervisor.app_error_in_log()
    assert error and "ModuleNotFoundError" in error


def test_a_healthy_app_reports_no_render_error(tmp_path):
    supervisor = _supervisor_with_log(
        tmp_path, "You can now view your Streamlit app in your browser.\n")
    assert supervisor.app_error_in_log() is None

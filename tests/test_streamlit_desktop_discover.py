"""Auto-detection of the things the operator should not have to type.

The rule these tests enforce: detect confidently or say so. A silently wrong
default (the wrong shell, the wrong entry script) surfaces as a mysterious
failure minutes into a 470 MB build — an honest "not found" plus a hint does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import discover


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "native_Provision"
    root.mkdir()
    return root


def make_shell(repo: Path, sibling: str = "nativeApp") -> Path:
    exe = repo.parent / sibling / "apps" / "host-tauri" / "prebuilt" / "cim-light.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return exe


def make_runtime(repo: Path) -> Path:
    runtime = repo / ".runtime-cache" / "python311"
    runtime.mkdir(parents=True)
    (runtime / "python.exe").write_bytes(b"MZ")
    return runtime


# ── shell ────────────────────────────────────────────────────────────────────

def test_shell_is_found_in_the_sibling_nativeapp_repo(repo, monkeypatch):
    monkeypatch.delenv("CIM_TAURI_EXE", raising=False)
    exe = make_shell(repo)
    found = discover.find_shell(repo)
    assert found.value == exe
    assert "nativeApp" in found.source          # the operator can see WHERE it came from


def test_shell_env_override_wins(repo, tmp_path, monkeypatch):
    make_shell(repo)
    other = tmp_path / "custom.exe"
    other.write_bytes(b"MZ")
    monkeypatch.setenv("CIM_TAURI_EXE", str(other))
    assert discover.find_shell(repo).value == other


def test_missing_shell_says_so_with_a_fix(repo, monkeypatch):
    monkeypatch.delenv("CIM_TAURI_EXE", raising=False)
    found = discover.find_shell(repo)
    assert not found.found
    assert "cim-light.exe" in found.hint and "WDAC" in found.hint


# ── runtime ──────────────────────────────────────────────────────────────────

def test_runtime_is_found_in_the_local_cache(repo, monkeypatch):
    monkeypatch.delenv("CIM_PORTABLE_PYTHON", raising=False)
    runtime = make_runtime(repo)
    assert discover.find_runtime(repo).value == runtime


def test_runtime_needs_a_python_exe_not_just_a_folder(repo, monkeypatch):
    monkeypatch.delenv("CIM_PORTABLE_PYTHON", raising=False)
    (repo / ".runtime-cache" / "python311").mkdir(parents=True)   # empty folder
    assert not discover.find_runtime(repo).found


def test_missing_runtime_points_at_the_download_button(repo, monkeypatch):
    monkeypatch.delenv("CIM_PORTABLE_PYTHON", raising=False)
    found = discover.find_runtime(repo)
    assert not found.found
    assert "下載可攜 Python" in found.hint


# ── entrypoint ───────────────────────────────────────────────────────────────

def test_entrypoint_prefers_the_conventional_name(tmp_path):
    (tmp_path / "app.py").write_text("import streamlit as st\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("import streamlit as st\n", encoding="utf-8")
    found = discover.find_entrypoint(tmp_path)
    assert found.value == tmp_path / "app.py"


def test_entrypoint_falls_back_to_the_only_file_that_renders_a_page(tmp_path):
    (tmp_path / "dashboard.py").write_text("import streamlit as st\nst.title('hi')\n",
                                           encoding="utf-8")
    (tmp_path / "helpers.py").write_text("import pandas\n", encoding="utf-8")
    found = discover.find_entrypoint(tmp_path)
    assert found.value == tmp_path / "dashboard.py"


def test_entrypoint_refuses_to_guess_between_two_candidates(tmp_path):
    (tmp_path / "one.py").write_text("import streamlit as st\nst.write('1')\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("import streamlit as st\nst.write('2')\n", encoding="utf-8")
    found = discover.find_entrypoint(tmp_path)
    assert not found.found                      # ask, do not flip a coin
    assert "one.py" in found.hint and "two.py" in found.hint


def test_entrypoint_says_when_this_is_not_a_streamlit_project(tmp_path):
    (tmp_path / "script.py").write_text("print('hi')\n", encoding="utf-8")
    found = discover.find_entrypoint(tmp_path)
    assert not found.found
    assert "import streamlit" in found.hint


def test_entrypoint_handles_a_missing_project_folder(tmp_path):
    assert not discover.find_entrypoint(tmp_path / "nope").found


# The real projects that broke this: the entry point is a level or two down
# (CV_Viewer keeps it in 5_PG_Develop/), and the repo is full of other files
# that import streamlit — spikes, tests, helper modules.

def test_entrypoint_is_found_in_a_subdirectory(tmp_path):
    nested = tmp_path / "5_PG_Develop"
    nested.mkdir()
    (nested / "app.py").write_text("import streamlit as st\nst.title('x')\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text("import pytest\n", encoding="utf-8")
    found = discover.find_entrypoint(tmp_path)
    assert found.value == nested / "app.py"
    assert "5_PG_Develop/app.py" in found.source        # shown as a relative path


def test_shallower_conventional_name_wins(tmp_path):
    (tmp_path / "app.py").write_text("import streamlit as st\nst.title('root')\n", encoding="utf-8")
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "app.py").write_text("import streamlit as st\nst.title('nested')\n", encoding="utf-8")
    assert discover.find_entrypoint(tmp_path).value == tmp_path / "app.py"


def test_spikes_and_tests_are_not_mistaken_for_the_entry_point(tmp_path):
    for folder in ("spike", "tests", ".venv/Lib/site-packages"):
        path = tmp_path / folder
        path.mkdir(parents=True)
        (path / "viewer_spike.py").write_text("import streamlit as st\nst.write('x')\n",
                                              encoding="utf-8")
    app = tmp_path / "5_PG_Develop"
    app.mkdir()
    (app / "app.py").write_text("import streamlit as st\nst.title('real')\n", encoding="utf-8")
    assert discover.find_entrypoint(tmp_path).value == app / "app.py"


def test_helper_modules_that_import_streamlit_are_not_entry_points(tmp_path):
    """viewer.py imports streamlit but only defines functions — not a page."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "viewer.py").write_text(
        "import streamlit as st\n\ndef render(img):\n    st.image(img)\n", encoding="utf-8")
    (src / "dashboard.py").write_text(
        "import streamlit as st\nst.set_page_config(layout='wide')\nst.title('go')\n",
        encoding="utf-8")
    found = discover.find_entrypoint(tmp_path)
    assert found.value == src / "dashboard.py"


def test_two_plausible_pages_ask_instead_of_guessing(tmp_path):
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_text("import streamlit as st\nst.title('x')\n", encoding="utf-8")
    found = discover.find_entrypoint(tmp_path)
    assert not found.found
    assert "one.py" in found.hint and "two.py" in found.hint


# ── name / output ────────────────────────────────────────────────────────────

def test_suggested_name_is_readable(tmp_path):
    project = tmp_path / "sales-dashboard"
    project.mkdir()
    assert discover.suggest_name(project) == "Sales Dashboard"


def test_default_output_lives_under_dist(repo):
    assert discover.default_output(repo) == repo / "dist" / "streamlit-apps"

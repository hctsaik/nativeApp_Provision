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


# ── the wrong-tab rescue, in BOTH directions ─────────────────────────────────
#
# The CIM tab has told Streamlit users "you are on the wrong tab" since day one.
# The Streamlit tab never returned the courtesy: point ANnoTation (18 CIM modules
# at modules/module_XXX/plugin.yaml, each of which imports streamlit) at it and it
# confidently offered four of those modules as candidate entry points — sending the
# operator to pick one, on a tab that could never build this project.

def _module_collection(root: Path, count: int = 3) -> Path:
    """ANnoTation's shape: plugin.yaml two levels down, and modules that DO import
    streamlit — so the entry-point search fails with "too many", not "none"."""
    for i in range(count):
        module = root / "modules" / f"module_{i:03d}"
        module.mkdir(parents=True)
        # Real ids: these fixtures get fed to discover_source_modules, which (rightly)
        # rejects a folder whose modules all claim the same id.
        (module / "plugin.yaml").write_text(
            f"id: module_{i:03d}\nversion: 1.0.0\n", encoding="utf-8")
        (module / f"{i:03d}_input.py").write_text(
            "import streamlit as st\nst.title('module')\n", encoding="utf-8")
    return root


def test_plugin_yaml_is_found_a_few_levels_down(tmp_path):
    """ANnoTation's 18 live at modules/module_XXX/plugin.yaml. A top-level-only
    look finds nothing and the rescue never fires."""
    _module_collection(tmp_path, count=18)
    hits = discover.find_plugin_manifests(tmp_path)
    assert len(hits) == 18


def test_a_cim_module_collection_on_the_streamlit_tab_is_told_which_tab_to_use(tmp_path):
    """The ANnoTation case, exactly: many streamlit-importing files, no single app,
    and a hint that said 「請用「瀏覽…」自行指定」 — inviting the operator to pick a
    CIM module as a Streamlit entry point. Name what the folder is and where it goes."""
    _module_collection(tmp_path, count=18)

    found = discover.find_entrypoint(tmp_path)

    assert not found.found
    assert "plugin.yaml" in found.hint                       # what this folder IS
    assert "CIM 平台模組" in found.hint                       # the tab that wants it
    assert discover.MODULE_ROOT_FIELD in found.hint          # the field that ACCEPTS it
    found.hint.encode("cp950")


def test_cim_modules_are_never_offered_as_streamlit_entry_point_candidates(tmp_path):
    """The hint used to open with 「找到多個可能的入口(modules/module_000/000_input.py…),
    請用「瀏覽…」自行指定」 and only THEN mention the wrong tab. The four "candidates"
    are CIM modules; picking one is exactly the mistake being rescued from. When the
    rescue fires it must replace that invitation, not queue up behind it."""
    _module_collection(tmp_path, count=18)

    hint = discover.find_entrypoint(tmp_path).hint

    assert "瀏覽" not in hint and "自行指定" not in hint     # no invitation to pick one
    assert "000_input.py" not in hint                        # not even listed as a candidate
    assert discover.MODULE_ROOT_FIELD in hint                # just the way out
    hint.encode("cp950")


def test_two_plausible_pages_still_get_the_invitation(tmp_path):
    """…and the rescue must not eat the normal answer: a real Streamlit project with
    two candidate pages needs 「瀏覽…」, which is the correct advice there."""
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_text("import streamlit as st\nst.title('x')\n", encoding="utf-8")
    hint = discover.find_entrypoint(tmp_path).hint
    assert "瀏覽" in hint and "one.py" in hint and "two.py" in hint


def test_the_rescue_names_a_field_that_actually_accepts_the_folder(tmp_path):
    """The rescue used to end with 「把「平台專案」指到這裡」. PlatformGateway requires
    <it>\\sidecar\\python-engine\\engine.py, which a module collection never has — so
    following our own advice hit a hard error 100% of the time.

    This test refuses to take the hint's word for it: it feeds the folder the hint
    names into the code behind that field, and the folder the OLD hint named into the
    code behind THAT field, and checks which one survives."""
    import sys

    from provision_builder.gateway import GatewayError, PlatformGateway
    from provision_builder.source_pack import discover_source_modules

    _module_collection(tmp_path, count=3)
    hint = discover.hint_for_streamlit_tab(tmp_path)

    # The old advice: this folder into 「平台專案」. Still a dead end — so the hint
    # must not send anyone there, and must say so out loud.
    with pytest.raises(GatewayError):
        PlatformGateway(tmp_path)
    assert f"把「{discover.PLATFORM_FIELD}」指到這裡" not in hint
    assert discover.PLATFORM_FIELD_IS_THE_PLATFORM in hint

    # The new advice: 「Module 資料夾」 → this exact path. Run it through the code that
    # field feeds, and it must actually load the modules.
    collection = discover.find_module_collection(tmp_path)
    assert collection.module_root == tmp_path / "modules"
    assert str(collection.module_root) in hint
    modules = discover_source_modules(collection.module_root, [sys.executable])
    assert len(modules) == 3
    hint.encode("cp950")


def test_a_plugin_yaml_in_tests_fixtures_does_not_suppress_the_streamlit_rescue(tmp_path):
    """The GUI kept its own depth-3 plugin.yaml glob with no skip list. One
    `tests/fixtures/plugin.yaml` — a fixture, not a module — was enough to make the
    tool declare a Streamlit project a CIM module collection, and the entire Streamlit
    rescue vanished: the operator was told to point 「Module 資料夾」 at their tests
    folder. SKIPPED_DIRS is the whole difference."""
    (tmp_path / "app.py").write_text("import streamlit as st\nst.title('real')\n", encoding="utf-8")
    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "plugin.yaml").write_text("id: not_a_module\n", encoding="utf-8")

    assert discover.find_plugin_manifests(tmp_path) == []
    assert not discover.find_module_collection(tmp_path).found

    hint = discover.hint_for_cim_tab(tmp_path)
    assert discover.STREAMLIT_TAB in hint                    # the rescue fires…
    assert str(fixtures) not in hint                         # …and never names the fixture folder
    assert discover.MODULE_ROOT_FIELD not in hint            # this is not a module collection
    hint.encode("cp950")


# ── where the modules live: one implementation, used by both tabs ─────────────

def test_module_root_is_the_layer_that_directly_holds_the_module_folders(tmp_path):
    _module_collection(tmp_path, count=18)
    collection = discover.find_module_collection(tmp_path)
    assert collection.found
    assert len(collection.manifests) == 18
    assert collection.module_root == tmp_path / "modules"     # not tmp_path, not a module
    assert not collection.is_single_module


def test_pointing_at_one_module_reports_the_layer_above_it(tmp_path):
    """source_pack refuses this folder; the answer it needs — the parent — is the
    same 'where do the modules live' calculation, so it lives in the same place."""
    module = tmp_path / "modules" / "module_001"
    module.mkdir(parents=True)
    (module / "plugin.yaml").write_text("id: m\n", encoding="utf-8")

    collection = discover.find_module_collection(module)
    assert collection.is_single_module
    assert collection.module_root == tmp_path / "modules"


def test_the_cim_tab_stays_quiet_when_the_folder_is_already_the_module_root(tmp_path):
    """「請把欄位改成它現在的值」 is what the previous version said. The fault is
    elsewhere (a broken plugin.yaml, the wrong platform); do not send them chasing it."""
    for i in range(3):
        module = tmp_path / f"module_{i:03d}"
        module.mkdir()
        (module / "plugin.yaml").write_text("id: m\n", encoding="utf-8")

    collection = discover.find_module_collection(tmp_path)
    assert collection.already_correct
    assert discover.hint_for_cim_tab(tmp_path) == ""


def test_one_stray_manifest_does_not_drag_the_operator_away_from_the_other_18(tmp_path):
    """Ties go to the layer holding the MOST modules."""
    _module_collection(tmp_path, count=18)                     # modules/module_XXX/plugin.yaml
    stray = tmp_path / "sandbox" / "one_off"
    stray.mkdir(parents=True)
    (stray / "plugin.yaml").write_text("id: stray\n", encoding="utf-8")

    assert discover.find_module_collection(tmp_path).module_root == tmp_path / "modules"


def test_a_streamlit_project_on_the_cim_tab_is_sent_to_the_streamlit_tab(tmp_path):
    (tmp_path / "app.py").write_text("import streamlit as st\nst.title('x')\n", encoding="utf-8")
    hint = discover.hint_for_cim_tab(tmp_path)
    assert discover.STREAMLIT_TAB in hint
    assert discover.SD_PROJECT_FIELD in hint
    assert str(tmp_path) in hint
    hint.encode("cp950")


# ── the folder pick must not freeze the UI ───────────────────────────────────

def test_the_venv_is_never_descended_into(tmp_path, monkeypatch):
    """`sorted(rglob("*.py"))` listed every .py in the project and filtered afterwards:
    on the real C:\\code\\claude\\AI4BI that is 9343 files, nearly all inside .venv —
    2.4–3.2 s (measured) of frozen Tk main thread on every folder pick, for the same
    103 candidates os.walk finds in 4–6 ms. Filtering after the walk is not the same
    as not walking — prune `dirnames` in place or os.walk has already gone in."""
    (tmp_path / "app.py").write_text("import streamlit as st\nst.title('x')\n", encoding="utf-8")
    deep = tmp_path / ".venv" / "Lib" / "site-packages" / "pandas"
    deep.mkdir(parents=True)
    (deep / "core.py").write_text("import streamlit as st\n", encoding="utf-8")

    visited: list[str] = []
    real_walk = discover.os.walk

    def spy(top, *args, **kwargs):
        for row in real_walk(top, *args, **kwargs):
            visited.append(str(row[0]))
            yield row

    monkeypatch.setattr(discover.os, "walk", spy)
    files = list(discover._searchable_py(tmp_path))

    assert tmp_path / "app.py" in files
    assert not any(".venv" in v for v in visited)      # never opened the door
    assert all(".venv" not in str(f) for f in files)


def test_files_deeper_than_max_depth_are_still_ignored(tmp_path):
    """Pruning must not quietly widen the search: the depth limit is what keeps a
    40 GB folder picked by mistake from being crawled."""
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "buried.py").write_text("import streamlit as st\nst.title('x')\n", encoding="utf-8")
    (tmp_path / "a" / "b" / "c" / "reachable.py").write_text("x = 1\n", encoding="utf-8")

    files = list(discover._searchable_py(tmp_path))
    assert tmp_path / "a" / "b" / "c" / "reachable.py" in files
    assert deep / "buried.py" not in files


def test_a_folder_with_neither_streamlit_nor_an_app_still_names_the_other_tab(tmp_path):
    """The empty dead end (no `import streamlit` anywhere) needs the same way out."""
    module = tmp_path / "modules" / "module_001"
    module.mkdir(parents=True)
    (module / "plugin.yaml").write_text("id: m\n", encoding="utf-8")
    (module / "run.py").write_text("print('no streamlit here')\n", encoding="utf-8")

    found = discover.find_entrypoint(tmp_path)

    assert not found.found
    assert "import streamlit" in found.hint                  # the original diagnosis
    assert "CIM 平台模組" in found.hint                       # …plus the rescue


def test_a_real_streamlit_project_is_not_sent_to_the_other_tab(tmp_path):
    """The rescue must not fire on a folder that has no plugin.yaml — a project with
    two plausible pages needs 「瀏覽…」, not a lecture about the CIM tab."""
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_text("import streamlit as st\nst.title('x')\n", encoding="utf-8")
    found = discover.find_entrypoint(tmp_path)
    assert "CIM 平台模組" not in found.hint


def test_a_platform_project_error_names_the_gui_field_not_the_cli_argument(tmp_path):
    """The other end of the same rescue. `PlatformGateway` told an operator staring
    at a GUI to fix 「build 的第一個參數」 — a CLI concept that appears nowhere on
    their screen. The field in front of them is labelled 「平台專案」."""
    from provision_builder.gateway import GatewayError, PlatformGateway

    with pytest.raises(GatewayError) as exc:
        PlatformGateway(tmp_path)                    # a folder with no engine.py

    message = str(exc.value)
    assert "平台專案" in message                      # the field they can see
    assert str(tmp_path) in message                   # and what it currently points at
    message.encode("cp950")


# ── name / output ────────────────────────────────────────────────────────────

def test_suggested_name_is_readable(tmp_path):
    project = tmp_path / "sales-dashboard"
    project.mkdir()
    assert discover.suggest_name(project) == "Sales Dashboard"


def test_default_output_lives_under_dist(repo):
    assert discover.default_output(repo) == repo / "dist" / "streamlit-apps"

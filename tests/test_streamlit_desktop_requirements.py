"""Where a project declares its dependencies, and where the app is run from.

Both of these were found by packaging real projects: AI4BI declares deps in
pyproject.toml (no requirements.txt at all) and uses package-absolute imports
(`from ai4bi.analysis...`), which only resolve when Streamlit is run from the
project ROOT — the way every Streamlit README tells you to run it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import requirements as req_mod

TEMPLATES = (Path(__file__).resolve().parents[1] / "src" / "provision_builder"
             / "streamlit_desktop" / "templates")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_tmpl_{name}", TEMPLATES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launch = _load("launch")


# ── requirements resolution ──────────────────────────────────────────────────

def test_plain_requirements_is_used_when_present(tmp_path):
    (tmp_path / "requirements.txt").write_text("streamlit>=1.0\n", encoding="utf-8")
    found = req_mod.resolve(tmp_path)
    assert found.path == tmp_path / "requirements.txt" and not found.generated


def test_lock_file_wins_over_plain_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("streamlit>=1.0\n", encoding="utf-8")
    (tmp_path / "requirements.lock.txt").write_text("streamlit==1.40.0\n", encoding="utf-8")
    found = req_mod.resolve(tmp_path)
    assert found.path.name == "requirements.lock.txt"


def test_pyproject_dependencies_are_used_when_there_is_no_requirements_file(tmp_path):
    """AI4BI's real shape: deps live in pyproject, nothing else exists."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "ai4bi"\ndependencies = [\n'
        '  "streamlit>=1.35",\n  "duckdb>=0.9",\n]\n', encoding="utf-8")
    staging = tmp_path / "staging"
    found = req_mod.resolve(tmp_path, staging=staging)

    assert found.generated and found.path.parent == staging
    body = found.path.read_text("utf-8")
    assert "streamlit>=1.35" in body and "duckdb>=0.9" in body
    assert "pyproject" in found.source


def test_pyproject_without_dependencies_says_so(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(req_mod.RequirementsError, match="沒有 \\[project\\].dependencies"):
        req_mod.resolve(tmp_path)


def test_no_declarations_at_all_is_an_actionable_error(tmp_path):
    with pytest.raises(req_mod.RequirementsError, match="找不到相依宣告"):
        req_mod.resolve(tmp_path)


def test_explicit_file_overrides_discovery(tmp_path):
    (tmp_path / "requirements.txt").write_text("streamlit>=1.0\n", encoding="utf-8")
    pinned = tmp_path / "custom.lock"
    pinned.write_text("streamlit==1.40.0\n", encoding="utf-8")
    assert req_mod.resolve(tmp_path, pinned).path == pinned


def test_pip_plumbing_is_stripped_before_pip_sees_it(tmp_path):
    """`pip freeze --all` on a python-build-standalone runtime writes pip as a
    file:// URL from the interpreter-builder's machine. Handing that to pip is
    an instant OSError — and every lock made the obvious way contains it."""
    lock = tmp_path / "requirements.lock.txt"
    lock.write_text(
        "# comment\n"
        "pip @ file:///D:/a/python-build-standalone/build/pip-24.1.2-py3-none-any.whl\n"
        "setuptools==69.0.0\n"
        "wheel==0.43.0\n"
        "streamlit==1.40.0\n"
        "opencv-python-headless==5.0.0.93\n", encoding="utf-8")

    for_pip = req_mod.sanitize_for_pip(lock, tmp_path / "staging")
    body = for_pip.read_text("utf-8")

    assert "pip @" not in body and "setuptools" not in body and "wheel==" not in body
    assert "streamlit==1.40.0" in body and "opencv-python-headless==5.0.0.93" in body
    assert lock.read_text("utf-8").count("pip @") == 1     # user's file untouched


def test_editable_and_local_path_lines_never_reach_pip(tmp_path):
    """`pip freeze` in the project's own venv emits the project itself as `-e .`
    or `ai4bi @ file:///C:/code/claude/AI4BI`. Both install nothing here and fail
    outright on the customer's machine — and the app's own source travels in
    application/ anyway."""
    lock = tmp_path / "requirements.lock.txt"
    lock.write_text(
        "streamlit==1.40.0\n"
        "-e .\n"
        "ai4bi @ file:///C:/code/claude/AI4BI\n"
        "./vendor/local-pkg\n"
        "pandas==2.2.0\n", encoding="utf-8")

    dropped: list[str] = []
    for_pip = req_mod.sanitize_for_pip(lock, tmp_path / "staging", progress=dropped.append)
    body = for_pip.read_text("utf-8")

    assert "-e ." not in body and "file://" not in body and "vendor" not in body
    assert "streamlit==1.40.0" in body and "pandas==2.2.0" in body
    assert any("不能在別台機器安裝" in line for line in dropped)   # and we said so


def test_a_vcs_dependency_is_not_dropped_as_if_it_were_the_project_itself(tmp_path):
    """`-e .` and `-e git+https://…/internal-lib.git` were dropped by the same
    branch, under the same message: 「專案自己的原始碼會直接打包進去」. For `-e .`
    that is true. For the git one it is a lie — internal-lib is somebody else's
    package, it is now simply ABSENT from the delivery, and the operator was told
    the opposite. Name the line, say why it cannot travel, say what to do."""
    lock = tmp_path / "requirements.txt"
    lock.write_text(
        "streamlit==1.40.0\n"
        "-e .\n"
        "-e git+https://github.com/acme/internal-lib.git#egg=internal-lib\n",
        encoding="utf-8")

    said: list[str] = []
    for_pip = req_mod.sanitize_for_pip(lock, tmp_path / "staging", progress=said.append)
    body = for_pip.read_text("utf-8")
    assert "git+" not in body and "-e ." not in body       # pip never sees either
    assert "streamlit==1.40.0" in body

    message = "\n".join(said)
    assert "internal-lib.git" in message                    # WHICH line
    assert "git" in message and "連網" in message           # WHY it cannot travel
    assert "pip wheel" in message and "==" in message       # WHAT to do instead
    # and the honest line about the project's own source is not claimed for it
    project_note = [s for s in said if "專案自己的原始碼" in s]
    assert project_note and "internal-lib" not in "".join(project_note)
    message.encode("cp950")


def test_a_project_with_no_declarations_is_pointed_at_the_lock_file_field(tmp_path):
    """"需要 requirements.txt" leaves an operator who HAS a lock file — just not in
    this folder — with nowhere to put it. The GUI grew a 「進階設定 → 相依 lock 檔」
    field for exactly this; the error that sends them looking must name it."""
    with pytest.raises(req_mod.RequirementsError) as exc:
        req_mod.resolve(tmp_path)

    message = str(exc.value)
    assert "進階設定" in message and "相依 lock 檔" in message
    assert "pip freeze" in message
    message.encode("cp950")


def test_optional_dependency_groups_can_be_opted_in(tmp_path):
    """AI4BI's real shape: `anthropic` sits in an `llm` extra and is imported
    lazily. An admin who wants the LLM path must be able to ask for it."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "ai4bi"\ndependencies = ["streamlit>=1.35"]\n'
        '[project.optional-dependencies]\n'
        'llm = ["anthropic>=0.40"]\ndev = ["pytest>=7"]\n', encoding="utf-8")

    groups = req_mod.pyproject_optional_dependencies(tmp_path / "pyproject.toml")
    assert groups == {"llm": ["anthropic>=0.40"], "dev": ["pytest>=7"]}

    plain = req_mod.resolve(tmp_path, staging=tmp_path / "s1")
    assert "anthropic" not in plain.path.read_text("utf-8")     # not by default

    opted = req_mod.resolve(tmp_path, staging=tmp_path / "s2", extras=("llm",))
    body = opted.path.read_text("utf-8")
    assert "streamlit>=1.35" in body and "anthropic>=0.40" in body
    assert "llm" in opted.source                                 # the log says why


def test_asking_for_an_extra_that_does_not_exist_is_an_actionable_error(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["streamlit>=1.35"]\n'
        '[project.optional-dependencies]\nllm = ["anthropic"]\n', encoding="utf-8")
    with pytest.raises(req_mod.RequirementsError, match="沒有這些 optional-dependencies"):
        req_mod.resolve(tmp_path, staging=tmp_path / "s", extras=("gpu",))


@pytest.mark.parametrize("line,expected", [
    ("pip @ file:///D:/x/pip.whl", "pip"),
    ("opencv-python-headless==5.0.0.93", "opencv-python-headless"),
    ("Streamlit[extra]>=1.35", "streamlit"),
    ("pandas ; python_version>'3.10'", "pandas"),
])
def test_distribution_name_extraction(line, expected):
    assert req_mod.distribution_name(line) == expected


@pytest.mark.parametrize("line,expected", [
    ("streamlit==1.40.0", True),
    ("streamlit>=1.35", True),
    ("Streamlit[extra]>=1", True),
    ("streamlit-aggrid==1.0", False),
    ("pandas==2.0", False),
])
def test_streamlit_declaration_detection(tmp_path, line, expected):
    path = tmp_path / "r.txt"
    path.write_text(f"# 中文註解\n{line}\n", encoding="utf-8")
    assert req_mod.declares_streamlit(path) is expected


# ── the app runs from its project root ───────────────────────────────────────

def test_streamlit_runs_from_the_project_root_not_the_script_folder(tmp_path):
    """`from ai4bi.x import y` only resolves when the root is the CWD/sys.path."""
    pkg_root = tmp_path / "pkg"
    entry = pkg_root / "application" / "ai4bi" / "ui" / "app.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("import streamlit as st\n", encoding="utf-8")
    (pkg_root / "runtime").mkdir()
    (pkg_root / "runtime" / "python.exe").write_bytes(b"MZ")
    (pkg_root / "launcher").mkdir()
    (pkg_root / "launcher" / "engine_shim.py").write_text("", encoding="utf-8")
    (pkg_root / "shell").mkdir()
    (pkg_root / "shell" / "cim-light.exe").write_bytes(b"MZ")
    (pkg_root / "app-package.json").write_text(
        '{"app_id":"app-ai4bi","display_name":"AI4BI",'
        '"entrypoint":"application/ai4bi/ui/app.py","python":"runtime/python.exe",'
        '"shell_executable":"shell/cim-light.exe","engine_shim":"launcher/engine_shim.py"}',
        encoding="utf-8")

    manifest = launch.load_manifest(pkg_root)
    assert manifest["_app_root"] == (pkg_root / "application").resolve()

    seen = {}

    class Fake:
        pid = 1
        returncode = 0

        def poll(self):
            return 0

    def popen(cmd, cwd=None, env=None, **_kw):
        seen["cwd"], seen["env"] = cwd, env
        return Fake()

    supervisor = launch.StreamlitSupervisor(
        {**manifest, "startup_timeout_seconds": 0.1, "preferred_port": 0},
        tmp_path, popen_factory=popen)
    with pytest.raises(launch.StreamlitExited):
        supervisor.start()

    assert Path(seen["cwd"]) == (pkg_root / "application").resolve()   # NOT .../ai4bi/ui
    assert str((pkg_root / "application").resolve()) in seen["env"]["PYTHONPATH"]

"""The defects a scenario review found — each one shipped a broken package or a
lie to the operator. These tests exist so they cannot come back.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import imports as imports_mod
from provision_builder.streamlit_desktop import pages as pages_mod
from provision_builder.streamlit_desktop import requirements as req_mod
from provision_builder.streamlit_desktop import validate as validate_mod
from provision_builder.streamlit_desktop.models import BuildRequest

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


def test_imports_in_a_function_the_module_body_calls_are_required(tmp_path):
    """The hole in "imports inside a def are lazy": it is only true of a def nobody
    calls at import time. `_setup()` at the bottom of the file runs while Streamlit
    is importing the script, so `import zzz_boom` in its body executes on the first
    render exactly like a module-level import — and the gate called it optional,
    passed the build, and the operator met it as a red traceback.

    Measured before the fix: required={'streamlit'}, optional={'zzz_boom'},
    blocking=[]."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text(
        "import streamlit as st\n"
        "\n"
        "def _setup():\n"
        "    import zzz_boom            # runs on first render: _setup() is called below\n"
        "    return zzz_boom\n"
        "\n"
        "def never_called():\n"
        "    import zzz_lazy           # genuinely lazy\n"
        "\n"
        "_setup()\n",
        encoding="utf-8")

    required, optional = imports_mod.classify(project, project / "app.py")
    assert set(required) == {"streamlit", "zzz_boom"}
    assert set(optional) == {"zzz_lazy"}

    report = imports_mod.missing_from_lock(project / "app.py", project, "streamlit==1.40.0\n")
    assert report.blocking == ["zzz_boom"]
    assert report.optional == ["zzz_lazy"]
    # and the operator is told WHY a function-body import is being called required
    assert "模組層被呼叫" in report.where("zzz_boom")
    report.failure_message().encode("cp950")


@pytest.mark.parametrize("body,required", [
    # called at the bottom of the file — the `_setup()` shape
    ("def boot():\n    import zzz_x\n\nboot()\n", True),
    # called inside a module-level `if` — still runs on import
    ("import os\n\ndef boot():\n    import zzz_x\n\nif os.name == 'nt':\n    boot()\n", True),
    # the return value is used at module level
    ("def boot():\n    import zzz_x\n    return zzz_x\n\nCONFIG = boot()\n", True),
    # a decorator that runs at import time IS a module-level call of the decorator
    ("def boot(fn):\n    import zzz_x\n    return fn\n\n@boot\ndef page():\n    pass\n", True),
    # nobody calls it: the AI4BI case, and it must stay optional
    ("def boot():\n    import zzz_x\n", False),
    # called only from inside another function — not at import time
    ("def boot():\n    import zzz_x\n\ndef outer():\n    boot()\n", False),
    # called under `if __name__ == '__main__'`: deliberately left optional (fail open)
    ("def boot():\n    import zzz_x\n\nif __name__ == '__main__':\n    boot()\n", False),
    # a guarded import inside a called function still degrades gracefully
    ("def boot():\n    try:\n        import zzz_x\n    except ImportError:\n        pass\n\nboot()\n",
     False),
])
def test_only_functions_the_module_body_really_runs_are_promoted(tmp_path, body, required):
    """The promotion must be exact in BOTH directions: missing a real module-level
    call ships a broken app, and promoting a def nobody calls resurrects the AI4BI
    false positive that made a working project unbuildable."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("import streamlit\n" + body, encoding="utf-8")

    names, lazy = imports_mod.classify(project, project / "app.py")
    assert ("zzz_x" in names) is required
    assert ("zzz_x" in lazy) is not required


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
    assert "加進" in message and "requirements" in message      # way out #1
    assert "確認它真的是選用的" in message                        # way out #2
    assert "try/except ImportError" in message          # …and how to say so honestly
    assert "一定跑不起來" not in message                 # not an assertion
    # the third "way out" that was never a fix: it only hid the import from us
    assert "移到函式內" not in message


def test_the_failure_message_never_tells_anyone_to_move_an_import_into_a_function(tmp_path):
    """It used to offer, as one of the two ways out: 「把 import 移到函式內」.

    That is not a fix. It moves the import to where this gate cannot see it — the
    package is still not installed, the code path still runs, and the app still dies,
    now on the factory floor with a green build behind it. We were teaching the
    operator to disable the check instead of fixing what it found. And since a
    function the module body calls is now REQUIRED anyway, the advice is not even
    reliably effective at the thing it was wrongly recommending."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("import streamlit\nimport duckdb\n", encoding="utf-8")
    message = imports_mod.missing_from_lock(project / "app.py", project,
                                            "streamlit==1.40.0\n").failure_message()

    assert "移到函式內" not in message
    assert "用到才 import" not in message
    # the two honest ways out, and only those
    assert "加進" in message and "requirements" in message
    assert "try/except ImportError" in message
    message.encode("cp950")


# ── the gate must not kill a build over its own alias table ──────────────────

@pytest.mark.parametrize("module,distribution", [
    # every one of these was reported MISSING while installed AND declared: the old
    # code fell through to "the import name must be the distribution name".
    ("Levenshtein", "python-Levenshtein==0.27.1"),
    ("psycopg2", "psycopg2-binary==2.9.9"),
    ("grpc", "grpcio==1.62.0"),
    ("cv2", "opencv-contrib-python==4.10.0.84"),
    ("MySQLdb", "mysqlclient==2.2.4"),
])
def test_a_declared_package_under_another_import_name_is_not_false_killed(
        tmp_path, module, distribution):
    """A false MISSING here does not cost six minutes — it makes the project
    unbuildable, with an error telling the operator to add a package they already
    declared and already have installed."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text(f"import streamlit\nimport {module}\n", encoding="utf-8")

    report = imports_mod.missing_from_lock(project / "app.py", project,
                                           f"streamlit==1.40.0\n{distribution}\n")
    assert report.blocking == [], report.failure_message()
    assert report.required == []


def test_the_interpreter_is_asked_before_our_own_alias_table(tmp_path):
    """`importlib.metadata.packages_distributions()` reads the installed packages'
    own manifests, so it cannot be stale the way a hand-written dict always is. It
    is the FIRST question, not a fallback — and it is what will map the next
    package nobody thought to add to the table."""
    resolved = imports_mod.resolve_distributions("yaml")     # PyYAML is installed here
    assert resolved.source == "metadata"
    assert "pyyaml" in resolved.candidates
    assert resolved.certain

    # a name no table and no interpreter knows is a GUESS, and it says so
    guessed = imports_mod.resolve_distributions("zzz_not_a_real_package")
    assert guessed.source == "guess" and not guessed.certain


def test_an_unrecognised_import_name_warns_instead_of_killing_the_build(tmp_path):
    """The general case behind Levenshtein/psycopg2/grpc: when we could only GUESS
    that the import name is the package name, and a declared package looks like it
    could be the real provider, we must not refuse the build. A gate that guesses
    fails OPEN — the post-install find_spec probe is ground truth and is still armed."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("import streamlit\nimport zzz_widget\n", encoding="utf-8")

    lock = "streamlit==1.40.0\npython-zzz-widget==3.1.0\n"        # not in any table
    report = imports_mod.missing_from_lock(project / "app.py", project, lock)

    assert report.complete is True          # a fully-pinned lock…
    assert report.blocking == []            # …and STILL not a reason to refuse
    assert report.unresolved == ["zzz_widget"]
    warning = "\n".join(report.warning_lines())
    assert "zzz_widget" in warning and "python-zzz-widget" in warning
    assert "安裝完成後" in warning           # what still guards it
    warning.encode("cp950")


def test_a_genuinely_absent_module_is_still_blocked_by_a_pinned_lock(tmp_path):
    """The fail-open rule must not become fail-always: when nothing declared even
    resembles the import, the answer is not a guess — it is a NO, and the whole
    point of this gate is to say so in a second instead of after six minutes."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("import streamlit\nimport zzz_nope\n", encoding="utf-8")

    report = imports_mod.missing_from_lock(project / "app.py", project,
                                           "streamlit==1.40.0\npandas==2.2.3\n")
    assert report.blocking == ["zzz_nope"]
    assert report.unresolved == []


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


# ── "not declared" is not "will not be installed" ────────────────────────────

def _ai4bi_shaped(project: Path) -> Path:
    """AI4BI in miniature: the app imports numpy at module level, and numpy is
    nowhere in [project].dependencies — it arrives with pandas."""
    (project / "analysis").mkdir(parents=True)
    (project / "app.py").write_text(
        "import streamlit as st\nfrom analysis import rfm\nst.title('x')\n",
        encoding="utf-8")
    (project / "analysis" / "__init__.py").write_text("", encoding="utf-8")
    (project / "analysis" / "rfm.py").write_text(
        "import numpy as np\nimport pandas as pd\n", encoding="utf-8")
    return project / "app.py"


def test_a_transitive_dependency_missing_from_pyproject_does_not_block_the_build(tmp_path):
    """The S6 blocker, exactly as measured on the real AI4BI: the app imports numpy,
    numpy is not in `pyproject [project].dependencies` — because pandas brings it —
    and the gate refused to build a project that builds. A direct-dependency list is
    NOT the transitive closure, and treating absence from it as proof of absence is
    a guess presented to the operator as a fact."""
    project = tmp_path / "ai4bi"
    entry = _ai4bi_shaped(project)
    pyproject_deps = "pandas>=2.0\nstreamlit>=1.35\nduckdb>=0.9\n"

    report = imports_mod.missing_from_lock(entry, project, pyproject_deps)

    assert report.required == ["numpy"]      # still noticed…
    assert report.blocking == []             # …but never a reason to refuse
    assert report.undeclared == ["numpy"]
    assert not report                        # `if missing:` callers must not fire

    warning = "\n".join(report.warning_lines())
    assert "numpy" in warning and "pandas" in warning          # who probably brings it
    assert "只宣告直接相依" in warning                          # why we are not sure
    assert "安裝完成後會再驗一次" in warning                     # what still guards it
    warning.encode("cp950")                                     # zh-TW console safe


def test_from_package_import_module_is_followed_into_the_module(tmp_path):
    """`from ai4bi.ui import workspace` — AI4BI's app.py line 34 — reaches
    ai4bi/ui/workspace.py, but we only ever opened ai4bi/ui/__init__.py. Every
    module-level import behind a line of that shape was invisible: the gate said
    「檢查通過」 and the missing package surfaced as a red traceback on the factory
    floor, which is the exact failure this module exists to prevent."""
    project = tmp_path / "proj"
    (project / "ui").mkdir(parents=True)
    (project / "app.py").write_text(
        "import streamlit as st\nfrom ui import workspace\nst.title('x')\n",
        encoding="utf-8")
    (project / "ui" / "__init__.py").write_text("", encoding="utf-8")     # empty!
    (project / "ui" / "workspace.py").write_text("import duckdb\n", encoding="utf-8")

    reached = imports_mod.runtime_sources(project, project / "app.py")
    assert project / "ui" / "workspace.py" in reached

    report = imports_mod.missing_from_lock(project / "app.py", project,
                                           "streamlit==1.40.0\n")
    assert report.blocking == ["duckdb"]


def test_a_module_missing_from_a_fully_pinned_lock_still_blocks_the_build(tmp_path):
    """The other half: a pinned lock IS the closure (that is what `pip freeze`
    emits, and what Store mode demands). A module that is not in it will not be
    installed, and finding that out now beats finding it out after a six-minute
    pip install. Softening this would throw away the whole point of the gate."""
    project = tmp_path / "ai4bi"
    entry = _ai4bi_shaped(project)
    lock = "pandas==2.2.3\nstreamlit==1.40.0\nduckdb==1.1.3\n"      # no numpy line

    report = imports_mod.missing_from_lock(entry, project, lock)

    assert report.blocking == ["numpy"]
    assert report.undeclared == []
    assert report                                        # `if missing:` DOES fire
    assert "numpy" in report.failure_message()


def test_a_pinned_lock_that_does_carry_the_module_is_clean(tmp_path):
    project = tmp_path / "ai4bi"
    entry = _ai4bi_shaped(project)
    lock = "numpy==2.1.3\npandas==2.2.3\nstreamlit==1.40.0\n"
    report = imports_mod.missing_from_lock(entry, project, lock)
    assert report.blocking == [] and report.required == []


@pytest.mark.parametrize("text,closure", [
    ("streamlit==1.40.0\npandas==2.2.3\n", True),
    ("Streamlit[extra]==1.40.0\n", True),
    ("streamlit==1.40.0 ; python_version >= '3.11'\n", True),
    # `pip freeze --all` on a python-build-standalone runtime always emits this;
    # it must not demote a real lock to a guess.
    ("streamlit==1.40.0\npip @ file:///D:/a/pip-24.1.2-py3-none-any.whl\n", True),
    ("# 註解\n\nstreamlit==1.40.0\n", True),
    # pip-compile's shape: a pinned line continued by --hash rows. Still a closure,
    # and reading it as "not a lock" would disarm the gate for the careful locker.
    ("numpy==2.4.6 \\\n    --hash=sha256:aaaa \\\n    --hash=sha256:bbbb\n"
     "streamlit==1.40.0 --hash=sha256:cccc\n", True),
    ("streamlit>=1.35\npandas>=2.0\n", False),          # pyproject-shaped
    ("streamlit==1.40.0\npandas\n", False),             # one bare name is enough
    ("streamlit==1.40.0\n-e .\n", False),
    ("streamlit==1.40.0\nlib @ git+https://x/y.git\n", False),
    ("", False),                                        # nothing pinned, nothing known
])
def test_is_pinned_closure_tells_a_lock_from_a_wish_list(text, closure):
    """The whole decision hangs off this: a pinned lock is the transitive closure
    and absence is proof; anything else lists what a human typed."""
    assert imports_mod.is_pinned_closure(text) is closure


def _request(tmp_path: Path, project: Path, entry: Path, **kw) -> BuildRequest:
    shell = tmp_path / "cim-light.exe"
    shell.write_bytes(b"MZ")
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "python.exe").write_bytes(b"MZ")
    return BuildRequest(project_dir=project, entrypoint=entry, display_name="AI4BI",
                        output_dir=tmp_path / "out", shell_exe=shell,
                        runtime_template=runtime, **kw)


def test_validate_does_not_refuse_a_pyproject_only_project_over_a_transitive_dep(tmp_path):
    """The S6 blocker seen from the GUI: 「檢查專案」 refused to let the operator
    build AI4BI at all. It must pass, and it must SAY the thing it is unsure about
    (a warning), rather than dress the guess up as a blocking error."""
    project = tmp_path / "ai4bi"
    entry = _ai4bi_shaped(project)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "ai4bi"\n'
        'dependencies = ["streamlit>=1.35", "pandas>=2.0", "duckdb>=0.9"]\n',
        encoding="utf-8")
    request = _request(tmp_path, project, entry)

    assert validate_mod.validate_request(request) == []      # the build may start

    warnings = validate_mod.warnings_for(request)
    assert any("numpy" in w and "只宣告直接相依" in w for w in warnings)


def test_validate_still_refuses_a_pinned_lock_that_is_missing_a_package(tmp_path):
    """…and the six-minute-pip-install saver is still armed where it can be trusted."""
    project = tmp_path / "ai4bi"
    entry = _ai4bi_shaped(project)
    (project / "requirements.lock.txt").write_text(
        "streamlit==1.40.0\npandas==2.2.3\nduckdb==1.1.3\n", encoding="utf-8")
    request = _request(tmp_path, project, entry)

    errors = validate_mod.validate_request(request)
    assert any("numpy" in e for e in errors)


def test_extras_that_a_lock_file_cannot_honour_are_not_dropped_in_silence(tmp_path):
    """The admin ticks 「llm」, the project ships a lock, and `resolve()` quietly
    ignores the group: they wait six minutes for an `anthropic` that was never
    going to be installed. Extras only mean something to pyproject — say so."""
    project = tmp_path / "ai4bi"
    entry = _ai4bi_shaped(project)
    (project / "requirements.lock.txt").write_text(
        "streamlit==1.40.0\npandas==2.2.3\nnumpy==2.1.3\nduckdb==1.1.3\n", encoding="utf-8")
    request = _request(tmp_path, project, entry, extras=("llm",))

    found = req_mod.resolve(project, extras=("llm",))
    assert found.ignored_extras == ("llm",)

    warnings = validate_mod.warnings_for(request)
    assert any("llm" in w and "不會生效" in w for w in warnings)
    assert validate_mod.validate_request(request) == []       # a warning, not a block


def _ai4bi_with_llm_extra(tmp_path: Path) -> tuple[Path, Path]:
    """AI4BI exactly: deps in pyproject, `anthropic` in the `llm` optional group,
    imported lazily inside the method that calls the model."""
    project = tmp_path / "ai4bi"
    project.mkdir()
    (project / "app.py").write_text(
        "import streamlit as st\n"
        "\n"
        "class Engine:\n"
        "    def _call_anthropic(self, prompt):\n"
        "        import anthropic\n"
        "        return anthropic.Anthropic()\n",
        encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "ai4bi"\ndependencies = ["streamlit>=1.35"]\n'
        '[project.optional-dependencies]\nllm = ["anthropic>=0.40"]\n',
        encoding="utf-8")
    return project, project / "app.py"


def test_advice_names_the_optional_group_instead_of_a_requirements_file(tmp_path):
    """「請加進 requirements」 is advice AI4BI's operator cannot follow: the project
    has no requirements.txt, and `anthropic` is ALREADY declared — in pyproject's
    `llm` optional group, which pip does not install unless you ask. The one action
    that works is the GUI field 「進階設定 → 選用相依群組」, and the message must
    name it and name the group."""
    project, entry = _ai4bi_with_llm_extra(tmp_path)
    request = _request(tmp_path, project, entry)

    warnings = validate_mod.warnings_for(request)
    said = "\n".join(warnings)

    assert "anthropic" in said
    assert "選用相依群組" in said and "llm" in said     # the field, and which group
    assert "加進 requirements.txt" not in said          # the advice that cannot be followed
    said.encode("cp950")
    assert validate_mod.validate_request(request) == []   # still only a warning


def test_a_module_level_call_of_a_group_only_package_still_names_the_group(tmp_path):
    """The same advice must hold when the import is REQUIRED, not lazy: `_setup()`
    at the bottom of the file calling `import anthropic` is a startup import, and
    the fix is still 「填 llm」 — not 「請加進 requirements」, which for a
    pyproject-only project points at a file that does not exist."""
    project = tmp_path / "ai4bi"
    project.mkdir()
    (project / "app.py").write_text(
        "import streamlit as st\n\ndef _setup():\n    import anthropic\n    return anthropic\n\n"
        "_setup()\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "ai4bi"\ndependencies = ["streamlit>=1.35"]\n'
        '[project.optional-dependencies]\nllm = ["anthropic>=0.40"]\n', encoding="utf-8")
    request = _request(tmp_path, project, project / "app.py")

    required, _optional = imports_mod.classify(project, project / "app.py")
    assert "anthropic" in required                    # the module body calls _setup()

    said = "\n".join(validate_mod.warnings_for(request))
    assert "選用相依群組" in said and "llm" in said
    assert "可能由其它套件帶進來" not in said          # we KNOW why it is missing
    said.encode("cp950")


def test_a_blocking_module_that_sits_in_an_optional_group_says_which_group(tmp_path):
    """Same lie, blocking side: the package is declared, in a group, and the build
    stops telling the operator to declare it."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("import streamlit\nimport anthropic\n", encoding="utf-8")
    (project / "requirements.lock.txt").write_text("streamlit==1.40.0\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "p"\ndependencies = ["streamlit>=1.35"]\n'
        '[project.optional-dependencies]\nllm = ["anthropic>=0.40"]\n', encoding="utf-8")

    found = req_mod.resolve(project)
    assert found.optional_groups() == {"llm": ("anthropic",)}

    report = validate_mod.missing_imports(_request(tmp_path, project, project / "app.py"), found)
    assert report.blocking == ["anthropic"]
    message = report.failure_message()
    assert "選用相依群組" in message and "llm" in message
    message.encode("cp950")


def test_store_mode_accepts_the_pip_freeze_output_it_told_the_operator_to_produce(tmp_path):
    """We tell the operator 「pip freeze > requirements.lock.txt」 in four places —
    including inside the very error they hit when they do it.

    `pip freeze`, run in a venv where the project was `pip install -e .`'d (the
    normal thing to do), emits `-e .` and `ai4bi @ file:///C:/code/claude/AI4BI`.
    normalize_lock rejected exactly those lines. So our own advice produced the
    thing we refused, with no way out: a closed loop with the operator inside it.

    Dropping them is not a workaround, it is correct — the project's own source
    travels in application/, so it installs nothing on the target and rightly has
    no place in the dependency fingerprint either."""
    project = tmp_path / "ai4bi"
    entry = _ai4bi_shaped(project)
    (project / "requirements.lock.txt").write_text(
        "streamlit==1.40.0\npandas==2.2.3\nnumpy==2.1.3\nduckdb==1.1.3\n"
        "-e .\n"
        f"ai4bi @ file:///{project.as_posix()}\n", encoding="utf-8")
    request = _request(tmp_path, project, entry)

    assert validate_mod.validate_store_request(request, "v1.0.0") == []


def test_somebody_elses_local_wheel_is_still_refused_and_says_what_to_do(tmp_path):
    """The other half. Silently dropping a THIRD-PARTY local wheel means that
    package is simply absent on the factory machine — the app dies there instead of
    here. Only the project's own source tree may be dropped."""
    from provision_builder.streamlit_desktop.device import runtime_store

    with pytest.raises(runtime_store.LockfileError) as caught:
        runtime_store.normalize_lock(
            "streamlit==1.40.0\nfoo @ file:///C:/wheels/foo-1.0-py3-none-any.whl\n")
    message = str(caught.value)
    assert "foo" in message                        # WHICH line
    assert "pip wheel" in message                  # WHAT to do about it
    message.encode("cp950")

    # …and a VCS editable stays refused too: the factory floor has no git, no network.
    with pytest.raises(runtime_store.LockfileError):
        runtime_store.normalize_lock(
            "streamlit==1.40.0\n-e git+https://github.com/x/y.git#egg=y\n")


def test_the_post_install_probe_is_always_authoritative(tmp_path, monkeypatch):
    """The staged interpreter has already had pip run against it, so what it cannot
    import will not be there at the factory. That report must keep blocking — the
    "maybe transitive" escape hatch belongs to the pre-check ONLY, and a default of
    complete=False here would quietly disarm the last gate before delivery."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("import streamlit\nimport numpy\n", encoding="utf-8")

    monkeypatch.setattr(imports_mod, "importable_in", lambda *_a, **_k: {"streamlit"})
    report = imports_mod.missing_dependencies(project / "app.py", project,
                                              tmp_path / "python.exe")
    assert report.complete is True
    assert report.blocking == ["numpy"] and bool(report) is True


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


def test_the_import_probe_does_not_write_bytecode_into_the_shared_runtime(monkeypatch, tmp_path):
    """The probe runs the SHARED runtime's own python.exe. Without -B it imports
    json/importlib and leaves stdlib __pycache__ *inside* that runtime — after its
    files.json was computed. The runtime then fails its own integrity check on every
    machine we deliver it to ("undeclared file: Lib/encodings/__pycache__/...").
    A read-only question must not leave fingerprints on what it asks about."""
    seen = {}

    class Result:
        returncode = 0
        stdout = '["streamlit"]'
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env") or {}
        return Result()

    monkeypatch.setattr(imports_mod.subprocess, "run", fake_run)
    imports_mod.importable_in(tmp_path / "python.exe", {"streamlit"})

    assert "-B" in seen["cmd"], "缺 -B:探測會把 .pyc 寫進共用 runtime"
    assert seen["env"].get("PYTHONDONTWRITEBYTECODE") == "1"


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


# ── the exclusion patterns nobody was checking ───────────────────────────────

def _excludable_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    (project / "data").mkdir(parents=True)
    (project / "recordings").mkdir()
    (project / "app.py").write_text("import streamlit as st\nst.title('x')\n", encoding="utf-8")
    (project / "helpers.py").write_text("def x(): pass\n", encoding="utf-8")
    (project / "data" / "rows.csv").write_text("a,b\n", encoding="utf-8")
    (project / "recordings" / "demo.mp4").write_bytes(b"\x00" * 16)
    (project / "requirements.txt").write_text("streamlit>=1.35\n", encoding="utf-8")
    return project, project / "app.py"


def test_an_exclude_pattern_that_would_drop_the_entrypoint_is_refused(tmp_path):
    """An entry script that is not in the package cannot start. validate never read
    extra_excludes at all, so this was invisible until someone opened the delivered
    folder — and the build said 建立完成."""
    project, entry = _excludable_project(tmp_path)
    request = _request(tmp_path, project, entry, extra_excludes=("app.py",))

    errors = validate_mod.validate_request(request)
    assert any("入口檔" in e for e in errors)
    assert any("app.py" in e for e in errors)          # names the pattern responsible
    "\n".join(errors).encode("cp950")


def test_an_exclude_pattern_that_drops_the_entrypoints_folder_is_refused(tmp_path):
    """copytree prunes a directory and never looks inside it, so excluding the
    FOLDER kills the entry script without any pattern ever naming it."""
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("import streamlit\n", encoding="utf-8")
    (project / "requirements.txt").write_text("streamlit>=1.35\n", encoding="utf-8")
    request = _request(tmp_path, project, project / "src" / "app.py",
                       extra_excludes=("src/",))

    assert any("入口檔" in e for e in validate_mod.validate_request(request))


def test_an_exclude_pattern_that_drops_every_py_file_is_refused(tmp_path):
    """THE shipped bug: one pattern (`data/*`, collapsed to `*`) excluded every file
    in the project, and the build reported 建立完成 — a delivered folder with no
    application code in it. The matcher is fixed; this makes the class unshippable."""
    project, entry = _excludable_project(tmp_path)
    request = _request(tmp_path, project, entry, extra_excludes=("*",))

    errors = validate_mod.validate_request(request)
    assert any("每一個" in e and ".py" in e for e in errors)
    "\n".join(errors).encode("cp950")


def test_an_exclude_pattern_that_matches_nothing_is_a_warning(tmp_path):
    """The only way a bad pattern can still hide: the operator types `recordigs/*`
    (a typo), the GUI accepts it, the report says the exclusion is in force, and the
    85 MB folder ships anyway. Nothing failed, so nothing was said."""
    project, entry = _excludable_project(tmp_path)
    request = _request(tmp_path, project, entry,
                       extra_excludes=("recordigs/*", "recordings/*"))

    warnings = validate_mod.warnings_for(request)
    typo = [w for w in warnings if "recordigs/*" in w]
    assert typo and "沒有比對到任何東西" in typo[0]
    # …and the one that DOES match is not nagged about
    assert not any("「recordings/*」" in w for w in warnings)
    assert validate_mod.validate_request(request) == []      # a warning, not a block
    "\n".join(warnings).encode("cp950")


def test_a_working_exclude_pattern_is_neither_an_error_nor_a_warning(tmp_path):
    """The gate must not become noise: the patterns that do their job say nothing."""
    project, entry = _excludable_project(tmp_path)
    (project / ".provisionignore").write_text("recordings/\n*.csv\n", encoding="utf-8")
    request = _request(tmp_path, project, entry)

    assert validate_mod.validate_request(request) == []
    assert not any("排除樣式" in w for w in validate_mod.warnings_for(request))


def test_the_exclusion_check_uses_the_builders_matcher_not_a_copy_of_it(tmp_path):
    """Two implementations of "is this file excluded" is exactly how the build side
    and the device side drifted apart. A Windows-style backslash pattern — what the
    operator actually types — must be understood here because it is understood
    THERE, not because we remembered to handle it twice."""
    project, entry = _excludable_project(tmp_path)
    request = _request(tmp_path, project, entry, extra_excludes=("recordings\\*",))

    # the builder's matcher normalises the separator; so, therefore, do we
    assert not any("沒有比對到任何東西" in w for w in validate_mod.warnings_for(request))


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


# ── the BUILD gate must see the pages the user can click ─────────────────────
#
# The device-side launcher has seen them for a while (its own tests live in
# test_streamlit_desktop_launcher.py). The build gate did not — and the build gate
# is the one that decides whether a package is delivered at all.

def _multipage_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    (project / "pages").mkdir(parents=True)
    (project / "app.py").write_text("import streamlit as st\nst.title('home')\n",
                                    encoding="utf-8")
    return project, project / "app.py"


def test_a_missing_import_in_a_page_does_not_pass_the_build_gate(tmp_path, monkeypatch):
    """THE blocker. Streamlit loads `pages/*.py` BY CONVENTION — nothing imports
    them — so a closure seeded with the entrypoint alone never opened the folder.
    Measured before the fix: a module-level `import zzz_nope` in pages/2_report.py
    produced blocking=[] warnings=[] from BOTH build-side gates. The broken build
    passed 「檢查專案」, passed the build, was delivered, was committed as
    last-known-good, and appeared as a red box the first time the operator clicked
    that page."""
    project, entry = _multipage_project(tmp_path)
    (project / "pages" / "2_report.py").write_text(
        "import streamlit as st\nimport zzz_nope\n", encoding="utf-8")

    # gate 1: the lock comparison, before pip has run
    report = imports_mod.missing_from_lock(entry, project, "streamlit==1.40.0\n")
    assert report.blocking == ["zzz_nope"]
    assert "2_report.py" in report.where("zzz_nope")          # and it says WHERE

    # gate 2: the post-install probe against the staged interpreter
    monkeypatch.setattr(imports_mod, "importable_in", lambda _py, wanted: {"streamlit"})
    probe = imports_mod.missing_dependencies(entry, project, tmp_path / "python.exe")
    assert probe.blocking == ["zzz_nope"]

    assert project / "pages" / "2_report.py" in imports_mod.runtime_sources(project, entry)


def test_a_lazy_import_in_a_page_is_still_only_a_warning(tmp_path):
    """Following the pages must not make the gate trigger-happy: an import inside a
    function body cannot break the first render of anything, page or not."""
    project, entry = _multipage_project(tmp_path)
    (project / "pages" / "2_report.py").write_text(
        "import streamlit as st\n\n\ndef export():\n    import zzz_lazy\n    return zzz_lazy\n",
        encoding="utf-8")

    report = imports_mod.missing_from_lock(entry, project, "streamlit==1.40.0\n")
    assert report.blocking == []
    assert report.optional == ["zzz_lazy"]


def test_a_helper_next_to_a_page_is_not_reported_as_a_missing_pypi_package(tmp_path):
    """The other direction, and the reason the launcher added the pages folder to
    its first-party roots: a `.py` sitting in `pages/` is the app's own code. Telling
    the admin to `pip install shared_bits` refuses a build that works — and the
    build gate must not now start doing what the launcher stopped doing."""
    project, entry = _multipage_project(tmp_path)
    (project / "pages" / "1_home.py").write_text(
        "import streamlit as st\nimport shared_bits\n", encoding="utf-8")
    (project / "pages" / "shared_bits.py").write_text("import zzz_deep\n", encoding="utf-8")

    report = imports_mod.missing_from_lock(entry, project, "streamlit==1.40.0\n")
    assert "shared_bits" not in report.blocking     # it is a page's helper, not PyPI
    assert report.blocking == ["zzz_deep"]          # …and we followed it INTO the helper


def test_a_page_declared_by_st_page_is_checked_by_the_build_gate(tmp_path):
    """st.navigation's pages need not live in pages/ — but the path is a literal
    string sitting in the entry script's AST, so there is no excuse for missing it."""
    project = tmp_path / "proj"
    (project / "screens").mkdir(parents=True)
    (project / "app.py").write_text(
        "import streamlit as st\n"
        "pg = st.navigation([st.Page('screens/report.py')])\npg.run()\n", encoding="utf-8")
    (project / "screens" / "report.py").write_text(
        "import streamlit as st\nimport zzz_nope\n", encoding="utf-8")

    report = imports_mod.missing_from_lock(project / "app.py", project, "streamlit==1.40.0\n")
    assert report.blocking == ["zzz_nope"]


def test_a_page_declared_in_pages_toml_is_checked_by_the_build_gate(tmp_path):
    """st-pages' .streamlit/pages.toml: literal paths, tomllib reads them."""
    project, entry = _multipage_project(tmp_path)
    (project / "screens").mkdir()
    (project / "screens" / "report.py").write_text("import zzz_nope\n", encoding="utf-8")
    (project / ".streamlit").mkdir()
    (project / ".streamlit" / "pages.toml").write_text(
        '[[pages]]\npath = "app.py"\nname = "Home"\n\n'
        '[[pages]]\npath = "screens/report.py"\nname = "Report"\n', encoding="utf-8")

    report = imports_mod.missing_from_lock(entry, project, "streamlit==1.40.0\n")
    assert report.blocking == ["zzz_nope"]


def test_a_runtime_built_page_list_is_not_pretended_to_be_covered(tmp_path):
    """The documented hole, kept honest. `st.Page(name)` where `name` is computed at
    runtime has no path to follow until the app runs, so no static gate can see it.
    We must neither crash on it nor invent a page — the log scan at the end of the
    session (launch.py) is what has to catch that one."""
    project, entry = _multipage_project(tmp_path)
    (project / "app.py").write_text(
        "import streamlit as st\n"
        "for name in ['screens/a.py']:\n    st.Page(name)\n", encoding="utf-8")
    (project / "screens").mkdir()
    (project / "screens" / "a.py").write_text("import zzz_nope\n", encoding="utf-8")

    assert pages_mod.declared_pages(entry, project) == []          # honestly blind
    assert imports_mod.missing_from_lock(entry, project, "streamlit==1.40.0\n").blocking == []


# ── one rulebook, both sides of the fence ────────────────────────────────────

def test_the_build_gate_and_the_device_launcher_share_one_page_rulebook():
    """Two implementations of "what does Streamlit actually load" is how the build
    side went blind while the launcher had it right. There is now exactly one file,
    and the launcher loads THAT file — not a copy of it."""
    module = launch.shared_pages()
    assert Path(module.__file__).resolve() == pages_mod.SOURCE
    assert module.MODULE_MARK == pages_mod.MODULE_MARK


def test_a_delivered_launcher_loads_the_page_rules_from_its_own_folder(tmp_path, monkeypatch):
    """On the device there is no provision_builder: pages.py is COPIED next to
    launch.py (launcher/pages.py) and loaded from there, by path. This is the load
    that actually runs on the factory floor."""
    launcher = tmp_path / "versions" / "1.0.0" / "launcher"
    launcher.mkdir(parents=True)
    shutil.copy2(pages_mod.SOURCE, launcher / "pages.py")
    monkeypatch.setattr(launch, "__file__", str(launcher / "launch.py"))
    monkeypatch.setattr(launch, "_PAGES_MODULE", None)

    module = launch.shared_pages()
    assert Path(module.__file__).resolve() == (launcher / "pages.py").resolve()
    assert module.MODULE_MARK == pages_mod.MODULE_MARK


def test_a_launcher_delivered_without_the_page_rules_refuses_to_run(tmp_path, monkeypatch):
    """If a builder ships launch.py without pages.py, the page half of the preflight
    would silently disappear — the exact blindness we just fixed, restored in the
    dark. Fail the version out loud instead (exit 4: this version's tree is wrong)."""
    launcher = tmp_path / "versions" / "1.0.0" / "launcher"
    launcher.mkdir(parents=True)
    monkeypatch.setattr(launch, "__file__", str(launcher / "launch.py"))
    monkeypatch.setattr(launch, "_PAGES_MODULE", None)

    with pytest.raises(launch.LauncherIncomplete) as excinfo:
        launch.shared_pages()
    message = str(excinfo.value)
    assert "pages.py" in message and "重新建置" in message
    message.encode("cp950")                       # a zh-TW console must be able to print it


def test_a_stray_pages_py_is_not_mistaken_for_the_page_rules(tmp_path, monkeypatch):
    """`pages.py` is a common file name. Loading whatever happens to carry it and
    calling it "the rules" would be a very quiet way to be wrong."""
    launcher = tmp_path / "launcher"
    launcher.mkdir(parents=True)
    (launcher / "pages.py").write_text("PAGES = ['a', 'b']\n", encoding="utf-8")
    monkeypatch.setattr(launch, "__file__", str(launcher / "launch.py"))
    monkeypatch.setattr(launch, "_PAGES_MODULE", None)

    with pytest.raises(launch.LauncherIncomplete):
        launch.shared_pages()

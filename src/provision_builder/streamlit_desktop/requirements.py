"""Where a project declares what it needs.

Not every project has a requirements.txt — AI4BI declares its dependencies in
`pyproject.toml [project].dependencies`, which is the modern default. Refusing
to package those would be our limitation masquerading as the project's fault.

Resolution order (first hit wins, and we tell the operator which one we used):
    requirements.lock.txt  →  requirements.txt  →  pyproject.toml [project]
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

LOCK_NAMES = ("requirements.lock.txt", "requirements-lock.txt")
PLAIN_NAMES = ("requirements.txt",)


class RequirementsError(Exception):
    pass


@dataclass(frozen=True)
class Requirements:
    path: Path            # the file to hand to pip (may be generated)
    source: str           # human-readable: where the declarations came from
    generated: bool       # True when we wrote `path` ourselves from pyproject
    # Extras the admin asked for that this source cannot honour. A lock file is
    # the whole truth by definition — there is nothing to opt into — but silently
    # dropping the group they ticked would leave them waiting six minutes for a
    # package that was never going to be installed. Say it instead.
    #
    # CALLERS: this is not decoration. If you resolve() with `extras` and do not
    # surface `ignored_extras`, the admin ticked a box that did nothing and nobody
    # told them. `validate.warnings_for()` renders it; a builder that resolves on
    # its own must do the same.
    ignored_extras: tuple[str, ...] = ()
    # The pyproject that backed this resolution, when one did — even if `path`
    # points at a generated requirements file in staging. Without it, "is the
    # missing package sitting in an optional-dependency group?" is unanswerable,
    # and the advice degrades to 「請加進 requirements」 for a project that has no
    # requirements.txt to add it to.
    pyproject: Path | None = None

    def optional_groups(self) -> dict[str, tuple[str, ...]]:
        """{group name: the distributions it declares} — `{"llm": ("anthropic",)}`.

        Only pyproject has these. It is what lets the import gate say 「在『進階設定
        → 選用相依群組』填 llm」 instead of telling the operator to add a package
        that their own pyproject already declares.
        """
        if self.pyproject is None or not Path(self.pyproject).is_file():
            return {}
        try:
            groups = pyproject_optional_dependencies(self.pyproject)
        except RequirementsError:
            return {}
        return {name: tuple(distribution_name(line) for line in lines
                            if distribution_name(line))
                for name, lines in groups.items()}


def _pyproject_data(pyproject: Path) -> dict:
    try:
        return tomllib.loads(Path(pyproject).read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RequirementsError(f"pyproject.toml 讀取失敗:{pyproject}({exc})") from exc


def pyproject_optional_dependencies(pyproject: Path) -> dict[str, list[str]]:
    """`[project.optional-dependencies]` — the extras a project offers.

    AI4BI keeps `anthropic` in an `llm` extra and imports it lazily, exactly so
    that mock-mode does not drag the SDK in. The admin who WANTS the LLM path
    must be able to say so; without reading this table we could only tell them
    "not declared", with no way to opt in.
    """
    groups = (_pyproject_data(pyproject).get("project") or {}).get("optional-dependencies")
    if not isinstance(groups, dict):
        return {}
    return {str(name): [str(d) for d in deps]
            for name, deps in groups.items() if isinstance(deps, list)}


def pyproject_dependencies(pyproject: Path, extras: tuple[str, ...] = ()) -> list[str]:
    """[project].dependencies, plus any optional-dependency groups the admin opted in."""
    pyproject = Path(pyproject)
    deps = (_pyproject_data(pyproject).get("project") or {}).get("dependencies")
    if not isinstance(deps, list) or not deps:
        raise RequirementsError(
            f"pyproject.toml 沒有 [project].dependencies:{pyproject}\n"
            "  請改用 requirements.txt,或在 pyproject 宣告相依。")
    lines = [str(d) for d in deps]
    if extras:
        groups = pyproject_optional_dependencies(pyproject)
        unknown = [name for name in extras if name not in groups]
        if unknown:
            raise RequirementsError(
                f"pyproject.toml 沒有這些 optional-dependencies 群組:{'、'.join(unknown)}\n"
                f"  這個專案可選的是:{'、'.join(groups) or '(沒有)'}")
        for name in extras:
            lines += groups[name]
    return lines


def resolve(project_dir: Path, explicit: Path | None = None,
            *, staging: Path | None = None, extras: tuple[str, ...] = ()) -> Requirements:
    """Find (or synthesize) the requirements file for a project.

    `staging=None` means "just tell me where the declarations are" — a read-only
    question. It must NOT write anything: the first version of this wrote
    `requirements.from-pyproject.txt` into the user's repository every time they
    pressed 「檢查專案」, which is a tool littering in someone else's project.

    `extras` only applies to the pyproject path: when a project ships a lock file,
    the lock is the truth and there is nothing to opt into.
    """
    project_dir = Path(project_dir)
    if explicit is not None:
        explicit = Path(explicit)
        if not explicit.is_file():
            raise RequirementsError(f"找不到指定的 requirements 檔:{explicit}")
        # An explicitly chosen pyproject.toml is still a pyproject: extras apply.
        is_pyproject = explicit.name == "pyproject.toml"
        lost = () if is_pyproject else tuple(extras)
        return Requirements(explicit, f"指定檔案:{explicit.name}", generated=False,
                            ignored_extras=lost,
                            pyproject=explicit if is_pyproject else None)

    # A lock/requirements file wins — but the project may STILL have a pyproject
    # whose optional groups explain a missing package. Carrying it costs nothing
    # and it is the difference between 「請加進 requirements」 and 「填 llm」.
    fallback_pyproject = project_dir / "pyproject.toml"
    known = fallback_pyproject if fallback_pyproject.is_file() else None

    for name in (*LOCK_NAMES, *PLAIN_NAMES):
        candidate = project_dir / name
        if candidate.is_file():
            return Requirements(candidate, f"專案的 {name}", generated=False,
                                ignored_extras=tuple(extras), pyproject=known)

    pyproject = project_dir / "pyproject.toml"
    if pyproject.is_file():
        deps = pyproject_dependencies(pyproject, extras)   # raises if there are none
        source = "pyproject.toml 的 [project].dependencies"
        if extras:
            source += f"(含選用群組:{'、'.join(extras)})"
        if staging is None:
            return Requirements(pyproject, source, generated=True, pyproject=pyproject)
        target = Path(staging) / "requirements.from-pyproject.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# 由 pyproject.toml 的相依宣告產生(建置用,勿手動編輯)\n"
            + "\n".join(deps) + "\n",
            encoding="utf-8")
        return Requirements(target, source, generated=True, pyproject=pyproject)

    raise RequirementsError(
        f"找不到相依宣告:{project_dir}\n"
        "  需要 requirements.txt、requirements.lock.txt,或 pyproject.toml 的 "
        "[project].dependencies。\n"
        "  兩條路,擇一即可:\n"
        "  1. 在專案的虛擬環境裡產一份:pip freeze > requirements.lock.txt\n"
        "     產完請看一下開頭幾行:如果專案自己是用 `pip install -e .` 裝的,"
        "freeze 會寫出 `-e .` 或 `你的專案 @ file:///…` 這種只在這台機器成立的行。"
        "把那一行刪掉即可——專案自己的原始碼會直接打包進交付包,不需要 pip 再裝一次。\n"
        "  2. 已經有現成的 lock 檔(在別的位置也可以):在「進階設定 → 相依 lock 檔」"
        "直接指定它,不必動到專案。")


def declared_names(found: "Requirements", extras: tuple[str, ...] = ()) -> list[str]:
    """The dependency lines, whichever source they came from."""
    if found.path.name == "pyproject.toml":
        return pyproject_dependencies(found.path, extras)
    return [line for line in found.path.read_text("utf-8", errors="replace").splitlines()
            if line.split("#", 1)[0].strip()]


def declared_text(found: "Requirements", extras: tuple[str, ...] = ()) -> str:
    """The declarations as one blob of text, for the checks that only need names."""
    return "\n".join(declared_names(found, extras))


# Packaging plumbing: we install and manage these ourselves. `pip freeze --all`
# on a python-build-standalone runtime emits pip as a LOCAL FILE URL from the
# machine that built the interpreter —
#   pip @ file:///D:/a/python-build-standalone/.../pip-24.1.2-py3-none-any.whl
# — and pip then tries to open a path that exists on nobody's disk. Every lock
# produced the obvious way carries this line.
PLUMBING = {"pip", "setuptools", "wheel"}


def distribution_name(line: str) -> str:
    """The package a requirement line names, whatever form it takes."""
    head = line.split("#", 1)[0].strip()
    for separator in (" @ ", "@", "==", ">=", "<=", "~=", "!=", ">", "<", "[", ";"):
        head = head.split(separator, 1)[0]
    return head.strip().lower().replace("_", "-")


_LOCAL_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|/)")


def is_unportable(line: str) -> bool:
    """True for a requirement line that only means anything on THIS machine.

    `pip freeze` in a project's own venv emits the project itself as either
    `-e .` or `ai4bi @ file:///C:/code/claude/AI4BI` — both of which install
    nothing on the operator's machine and fail outright on anyone else's. The
    app's own package travels in `application/`, so dropping these is exactly
    right; carrying them is a guaranteed `pip install` failure.
    """
    head = line.split("#", 1)[0].strip()
    if not head:
        return False
    if head.startswith(("-e", "--editable")):
        return True
    if "file://" in head:
        return True
    if _LOCAL_PATH.match(head):                       # a bare path: `.`, `./pkg`, `C:\pkg`
        return True
    return head in (".", "..")


_ARTIFACT_SUFFIXES = (".whl", ".zip", ".tar", ".gz", ".bz2", ".egg")


def _local_target(text: str) -> Path | None:
    """The path a `file://` URL or a bare local path points at, or None."""
    if text.startswith("file://"):
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        try:
            return Path(url2pathname(urlparse(text).path))
        except (ValueError, OSError):
            return None
    if _LOCAL_PATH.match(text) or text in (".", ".."):
        return Path(text)
    return None


def is_project_self(line: str, project_dir: Path | None = None) -> bool:
    """True when this unportable line IS the project we are packaging.

    `pip freeze` in the project's own venv writes the project as `-e .` or
    `ai4bi @ file:///C:/code/claude/AI4BI`. That one is free to drop: the source
    travels in `application/` and is never pip-installed.

    But `internal-lib @ file:///C:/wheels/internal_lib-1.0-py3-none-any.whl` is
    SOMEBODY ELSE's package, sitting on this build machine. Dropping it makes it
    simply ABSENT at the factory — and it was being dropped under the very same
    message, 「專案自己的原始碼會直接打包進去」, which is a lie about a package the
    operator now has no idea they lost. Same bug the VCS lines already had; this is
    the other half of it.
    """
    head = line.split("#", 1)[0].strip()
    if not head:
        return False
    if head.startswith(("-e", "--editable")):
        parts = head.split(None, 1)
        head = parts[1].strip() if len(parts) > 1 else "."
    if " @ " in head:
        head = head.split(" @ ", 1)[1].strip()
    elif "@" in head and "://" in head:
        head = head.split("@", 1)[1].strip()

    target = _local_target(head)
    if target is None:
        return False
    if target.suffix.lower() in _ARTIFACT_SUFFIXES:
        return False                      # a vendored wheel/sdist is not a source tree
    if head in (".", "..") or head.startswith(("./", "../", ".\\", "..\\")):
        return True                       # relative = relative to the project
    if project_dir is None:
        # A directory with no project to compare against: `pip freeze`'s shape for
        # the project itself. Assume the benign reading — the actionable advice
        # below is still printed for anything that looks like a package artifact.
        return True
    try:
        root, resolved = Path(project_dir).resolve(), target.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


_VCS_SCHEME = re.compile(r"(?:^|[\s@=])(?:git|hg|svn|bzr)\+", re.IGNORECASE)


def is_vcs(line: str) -> bool:
    """True for a requirement pip would have to CLONE — `git+https://…`,
    `-e git+ssh://…`, `pkg @ git+https://…`.

    Worth telling apart from `-e .`: `-e .` is the project itself, and the project
    travels in `application/`, so dropping it costs nothing. A VCS dependency is
    somebody else's package, and dropping it means it is simply not there.
    """
    head = line.split("#", 1)[0].strip()
    return bool(_VCS_SCHEME.search(head))


def sanitize_for_pip(requirements: Path, staging: Path, progress=None, *,
                     project_dir: Path | None = None) -> Path:
    """A copy of the requirements with the lines pip cannot use dropped, written
    where pip can read it. We never edit the user's file.

    THREE kinds of dropped line, and conflating them was a lie every time:

      · the project itself (`-e .`, `ai4bi @ file:///C:/code/AI4BI`) — packaged
        anyway, in `application/`. Free to drop, and we say so.
      · pip/setuptools/wheel — we install and manage those ourselves.
      · somebody else's local or VCS package (`-e git+https://…/internal-lib.git`,
        `internal-lib @ file:///C:/wheels/internal_lib-1.0.whl`) — this one is now
        simply ABSENT from the delivery, and the operator used to be told, in the
        same breath, that "專案自己的原始碼會直接打包進去". It gets the truth and,
        more to the point, a NEXT STEP: pin a version, or vendor a wheel.

    Pass `project_dir` and the third case is decided exactly rather than by shape.
    """
    lines = Path(requirements).read_text("utf-8", errors="replace").splitlines()
    kept, self_lines, plumbing_lines, foreign_lines, vcs_lines = [], [], [], [], []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
        elif is_vcs(stripped) and (stripped.startswith(("-e", "--editable"))
                                   or is_unportable(stripped)):
            vcs_lines.append(stripped)
        elif distribution_name(line) in PLUMBING:
            plumbing_lines.append(stripped)
        elif is_unportable(line):
            if is_project_self(line, project_dir):
                self_lines.append(stripped)
            else:
                foreign_lines.append(stripped)
        else:
            kept.append(line)

    if progress is not None:
        if self_lines:
            progress("已略過不能在別台機器安裝的相依行(專案自己的原始碼會直接打包進去):")
            for line in self_lines:
                progress(f"    - {line}")
        if plumbing_lines:
            progress("已略過打包工具自己的相依行(pip / setuptools / wheel 由我們安裝與管理):")
            for line in plumbing_lines:
                progress(f"    - {line}")
        for line in foreign_lines:
            progress(f"略過了一行指向這台建置機的相依:{line}")
            progress("      為什麼不能帶:這一行指的是本機磁碟上的檔案或資料夾。"
                     "交付包到了工廠現場,那個路徑不存在——這個套件到了現場就是不存在,"
                     "而它不是專案自己的原始碼,不會被打包進 application/。")
            progress("      怎麼辦:(1) 如果 PyPI 上有,改成釘死的版本,例如 internal-lib==1.2.3;"
                     "(2) 如果沒有,在這台機器 `pip wheel <這行>` 產出 .whl,"
                     "把 wheel 放進專案自帶的 wheelhouse 後改指向它(相對路徑,跟著專案走)。")
            progress("      如果 App 在啟動時就 import 它,建置會在安裝後的 import 檢查停下來。")
        for line in vcs_lines:
            progress(f"略過了一行 editable / VCS 相依:{line}")
            progress("      為什麼不能帶:editable 安裝只會在交付包裡留下一條指回這台建置機的路徑,"
                     "而 git+ 相依要在安裝時 clone——工廠現場的機器沒有 git、也不能連網,"
                     "這個套件到了現場就是不存在。")
            progress("      怎麼辦:(1) 改成釘死的版本,例如 internal-lib==1.2.3;"
                     "(2) 或先在這台機器 `pip wheel <這行>` 產出 .whl,"
                     "把 wheel 放進專案自帶的 wheelhouse 後改指向它。")
            progress("      如果 App 在啟動時就 import 它,建置會在安裝後的 import 檢查停下來。")

    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / "requirements.build.txt"
    target.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return target


def declares_streamlit(requirements: Path) -> bool:
    path = Path(requirements)
    if path.name == "pyproject.toml":
        lines = pyproject_dependencies(path)
    else:
        lines = path.read_text("utf-8", errors="replace").splitlines()
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if distribution_name(line) == "streamlit":
            return True
    return False

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
        return Requirements(explicit, f"指定檔案:{explicit.name}", generated=False)

    for name in (*LOCK_NAMES, *PLAIN_NAMES):
        candidate = project_dir / name
        if candidate.is_file():
            return Requirements(candidate, f"專案的 {name}", generated=False)

    pyproject = project_dir / "pyproject.toml"
    if pyproject.is_file():
        deps = pyproject_dependencies(pyproject, extras)   # raises if there are none
        source = "pyproject.toml 的 [project].dependencies"
        if extras:
            source += f"(含選用群組:{'、'.join(extras)})"
        if staging is None:
            return Requirements(pyproject, source, generated=True)
        target = Path(staging) / "requirements.from-pyproject.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# 由 pyproject.toml 的相依宣告產生(建置用,勿手動編輯)\n"
            + "\n".join(deps) + "\n",
            encoding="utf-8")
        return Requirements(target, source, generated=True)

    raise RequirementsError(
        f"找不到相依宣告:{project_dir}\n"
        "  需要 requirements.txt、requirements.lock.txt,或 pyproject.toml 的 [project].dependencies。")


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


def sanitize_for_pip(requirements: Path, staging: Path, progress=None) -> Path:
    """A copy of the requirements with the lines pip cannot use dropped, written
    where pip can read it. We never edit the user's file."""
    lines = Path(requirements).read_text("utf-8", errors="replace").splitlines()
    kept, dropped = [], []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
        elif distribution_name(line) in PLUMBING or is_unportable(line):
            dropped.append(stripped)
        else:
            kept.append(line)
    if dropped and progress is not None:
        progress("已略過不能在別台機器安裝的相依行(專案自己的原始碼會直接打包進去):")
        for line in dropped:
            progress(f"    - {line}")
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

"""Pre-flight checks. Every failure here is one the admin can act on, and every
one of them is cheaper to hit now than after a 300 MB build.

Fail closed (spec §3.1): a missing shell or a Streamlit-less requirements file
stops the build; we never quietly degrade the product.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import imports as imports_mod
from . import requirements as requirements_mod
from .models import BuildRequest

# Matches the distribution name at the head of a requirements line, so
# `streamlit==1.40.0`, `streamlit>=1.0`, and bare `streamlit` all count, while
# `streamlit-aggrid` does not.
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def declared_packages(requirements_text: str) -> set[str]:
    names = set()
    for raw in requirements_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = _REQ_NAME.match(line)
        if match:
            names.add(match.group(1).lower().replace("_", "-"))
    return names


def missing_imports(request: BuildRequest,
                    found: "requirements_mod.Requirements") -> imports_mod.MissingReport:
    """The import gate, answered from the declarations alone.

    This is a string comparison between the app's module-level imports and the
    names in the lock: no interpreter, no pip, no network — about a second on a
    real project. It used to run only AFTER a six-minute pip install, and only
    when the runtime was NOT reused, so the operator could be told "this cannot
    start" by a build they had already waited six minutes for — or not told at
    all. It belongs in 「檢查專案」.

    What comes back is deliberately two-sided (see imports.MissingReport):
    `.blocking` only when the declarations are a fully-pinned lock and absence is
    therefore proof; `.undeclared` — a warning — when they are a direct-dependency
    list and the module may still arrive transitively.
    """
    try:
        text = requirements_mod.declared_text(found, request.extras)
    except requirements_mod.RequirementsError:
        return imports_mod.MissingReport()
    return imports_mod.missing_from_lock(request.entrypoint, request.project_dir, text)


def warnings_for(request: BuildRequest) -> list[str]:
    """Everything 「檢查專案」 should SAY but must not fail on.

    The counterpart of `validate_request`: that one returns the reasons a build
    cannot start, this one the things the operator wants to know before waiting
    six minutes — an undeclared module that will probably arrive transitively, a
    lazy import nothing provides. Returning them from a separate call is what
    lets the GUI render them as ⚠ instead of ✗; folding them into the error list
    is exactly the lie this pair exists to undo.
    """
    if not request.project_dir.is_dir() or not request.entrypoint.is_file():
        return []
    try:
        found = requirements_mod.resolve(request.project_dir,
                                         request.explicit_requirements,
                                         extras=request.extras)
    except requirements_mod.RequirementsError:
        return []

    notes: list[str] = []
    if found.ignored_extras:
        notes.append(
            f"你勾的選用群組({'、'.join(found.ignored_extras)})這次不會生效:"
            f"相依來源是「{found.source}」,而選用群組只對 pyproject.toml 的 "
            "[project.optional-dependencies] 有意義——lock 檔本身就已經是完整的相依清單。"
            "要帶這些套件,請把它們加進 lock 檔。")
    return notes + missing_imports(request, found).warning_lines()


def is_inside(root: Path, candidate: Path) -> bool:
    """True when candidate lives under root. Both must already be resolved, so
    `..` traversal is gone by the time we compare."""
    root, candidate = root.resolve(), candidate.resolve()
    return candidate == root or root in candidate.parents


def validate_store_request(request: BuildRequest, version: str,
                           root: Path | None = None) -> list[str]:
    """The extra rules Store mode adds. They used to surface only when the build
    blew up minutes later — 「檢查專案」 said everything was fine."""
    from .device.identifiers import IdentifierError, validate_identifier
    from .device.runtime_store import LockfileError, normalize_lock

    errors = validate_request(request)
    if errors:
        return errors

    try:
        validate_identifier(version, "版本號")
    except IdentifierError:
        return [f"版本號不合法:{version!r}(只能用英數、`.`、`-`、`_`,例如 v1.0.0)"]

    found = requirements_mod.resolve(request.project_dir, request.explicit_requirements)
    if found.generated:
        return ["Store 佈局需要「完全釘死」的相依(每個套件都是 name==version)。\n"
                f"  這個專案的相依來自 {found.source},版本是範圍而不是釘死的。\n"
                "  解法:在專案自己的環境裡執行 → pip freeze > requirements.lock.txt"]
    try:
        pins = normalize_lock(found.path.read_text("utf-8", errors="replace"))
    except LockfileError as exc:
        return [str(exc)]
    if not any(p.startswith("streamlit==") for p in pins):
        return [f"lock 檔({found.path.name})沒有釘死 streamlit==<版本>"]

    if root is not None:
        version_dir = Path(root) / "apps" / request.app_id / "versions" / version
        if (version_dir / ".complete").is_file():
            errors.append(
                f"版本 {version} 在這棵 Store 樹裡已經建過了(版本目錄不可變)。\n"
                "  要發新版請換版本號;要重來請換一個乾淨的輸出根目錄。")
    return errors


def validate_request(request: BuildRequest) -> list[str]:
    errors: list[str] = []

    if not request.display_name.strip():
        errors.append("應用名稱不可空白。")

    if not request.project_dir.is_dir():
        errors.append(f"專案資料夾不存在:{request.project_dir}")
    else:
        if not request.entrypoint.is_file():
            errors.append(f"入口檔不存在:{request.entrypoint}")
        elif request.entrypoint.suffix.lower() != ".py":
            errors.append(f"入口檔必須是 .py:{request.entrypoint}")
        elif not is_inside(request.project_dir, request.entrypoint):
            errors.append(
                f"入口檔必須位於專案資料夾內:{request.entrypoint} 不在 {request.project_dir} 之下"
            )

        try:
            found = requirements_mod.resolve(request.project_dir,
                                             request.explicit_requirements,
                                             extras=request.extras)
        except requirements_mod.RequirementsError as exc:
            errors.append(str(exc))
        else:
            if not requirements_mod.declares_streamlit(found.path):
                errors.append(
                    f"相依宣告裡沒有 streamlit({found.source})——交付包不會在 User 端上網補裝。"
                )
            if request.entrypoint.is_file():
                report = missing_imports(request, found)
                # `.blocking`, not `.required`: a name absent from a fully-pinned
                # lock really will be missing, but a name absent from pyproject's
                # [project].dependencies may simply be somebody else's transitive
                # dependency (AI4BI imports numpy and declares pandas — numpy
                # arrives with it). Blocking on that made a working project
                # unbuildable; it is a warning (see warnings_for) and the
                # post-install probe in builder.py stays as the real gate.
                if report.blocking:
                    errors.append(report.failure_message())

    if not request.shell_exe.is_file():
        errors.append(
            f"找不到預建 Tauri 殼:{request.shell_exe}\n"
            "  取得方式見 nativeApp\\apps\\host-tauri\\prebuilt\\README.md(本機不可重編)"
        )

    runtime_python = request.runtime_template / "python.exe"
    if not runtime_python.is_file():
        errors.append(
            f"找不到可攜 Python runtime:{runtime_python}\n"
            "  產生方式:powershell -File nativeApp\\scripts\\win\\fetch-standalone-python.ps1 "
            "-DestRoot <目的地> -Flatten"
        )

    if request.preferred_port and not (1 <= request.preferred_port <= 65535):
        errors.append(f"preferred_port 超出範圍:{request.preferred_port}(0 = 交給系統隨機挑)")

    return errors

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

    The source and the project's optional-dependency groups are handed over with
    it, because the ADVICE depends on them: telling an AI4BI operator 「請加進
    requirements」 when the project has no requirements.txt and `anthropic` is
    already sitting in its pyproject `llm` extra is advice that cannot be followed.
    With the groups, the gate can name the GUI field that actually installs it.
    """
    try:
        text = requirements_mod.declared_text(found, request.extras)
    except requirements_mod.RequirementsError:
        return imports_mod.MissingReport()
    return imports_mod.missing_from_lock(
        request.entrypoint, request.project_dir, text,
        source_label=found.source,
        optional_groups=found.optional_groups())


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

    # Said whatever happens to the requirements: a pattern that silently matches
    # nothing is not less wrong because the project also has no lock file, and
    # returning [] here is how it stayed invisible.
    notes: list[str] = list(exclusion_warnings(request))
    try:
        found = requirements_mod.resolve(request.project_dir,
                                         request.explicit_requirements,
                                         extras=request.extras)
    except requirements_mod.RequirementsError:
        return notes

    if found.ignored_extras:
        notes.append(
            f"你勾的選用群組({'、'.join(found.ignored_extras)})這次不會生效:"
            f"相依來源是「{found.source}」,而選用群組只對 pyproject.toml 的 "
            "[project.optional-dependencies] 有意義——lock 檔本身就已經是完整的相依清單。"
            "要帶這些套件,請把它們加進 lock 檔。")
    return notes + missing_imports(request, found).warning_lines()


# We tell the operator 「pip freeze > requirements.lock.txt」 in four different
# places — and `pip freeze`, run in a venv where the project was installed with
# `pip install -e .` (the normal thing to do), emits the project itself as `-e .`
# or `ai4bi @ file:///C:/code/claude/AI4BI`. Store mode's normalize_lock then
# rejects exactly those lines. Sending someone down a path we ourselves refuse to
# accept, and saying only what is wrong with the result, is a dead end.
_PIP_FREEZE_CAVEAT = (
    "  注意:如果專案自己是用 `pip install -e .` 裝進那個環境的,pip freeze 會多寫出\n"
    "  一行 `-e .` 或 `你的專案名稱 @ file:///…`。那一行請直接刪掉——它指的是這台\n"
    "  建置機上的路徑,在別台機器不成立,而專案自己的原始碼本來就會直接打包進交付包,\n"
    "  不需要 pip 再裝一次。"
)


def _next_step_for_lock_error(lockfile: Path) -> str:
    """Turn normalize_lock's 「這一行不行」 into 「這一行不行,所以你要做這件事」.

    Two shapes, two different actions, and the operator cannot be expected to know
    which is which:
      · the project's own self-reference  -> delete the line, it is already packaged
      · somebody else's local/VCS package -> pin a version, or vendor a wheel
    """
    try:
        lines = Path(lockfile).read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    own, foreign = [], []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if requirements_mod.is_vcs(line):
            foreign.append(line)
        elif requirements_mod.is_unportable(line):
            (own if requirements_mod.is_project_self(line, lockfile.parent)
             else foreign).append(line)

    steps = []
    if own:
        steps.append(
            "\n  這幾行是「專案自己」,pip freeze 一定會寫出來,直接從 lock 檔刪掉即可"
            "(專案的原始碼會直接打包進交付包,不需要 pip 再裝一次):")
        steps += [f"      {line}" for line in own]
    if foreign:
        steps.append(
            "\n  這幾行指向這台建置機上的檔案或 git,交付到工廠現場就不存在了。"
            "請改成:(1) 釘死的版本,例如 internal-lib==1.2.3;或 (2) 先 `pip wheel <這行>` "
            "產出 .whl,放進專案自帶的 wheelhouse 再以相對路徑指向它:")
        steps += [f"      {line}" for line in foreign]
    return "\n".join(steps)


# ── the exclusion patterns nobody was checking ───────────────────────────────
#
# `.provisionignore` and the GUI's 「額外排除」 field both feed
# BuildRequest.extra_excludes, and validate read NEITHER. A pattern could exclude
# the entry script, or — as actually shipped — collapse to `*` and exclude every
# file in the project, and the build still said 建立完成: a delivered folder with
# no application code in it. The matcher itself is fixed; this makes that CLASS of
# mistake unshippable.
#
# Everything below asks builder.ignore_reason() for the answer. It does not
# re-implement the rule: two implementations of "is this file excluded" is exactly
# how the build side and the device side drifted apart, and how a pattern could be
# accepted, look applied, and do nothing.


def _exclusion_reason(project_dir: Path, patterns, target: Path) -> str | None:
    """Why `target` would not be copied — including because an ANCESTOR is dropped.

    copytree prunes a directory and never looks inside it, so `data/` excluded means
    `data/app.py` is gone even though no pattern names it. Checking only the file's
    own name would have missed exactly the bug we are guarding against.
    """
    from . import builder as builder_mod

    try:
        parts = Path(target).relative_to(project_dir).parts
    except ValueError:
        return None
    for depth in range(1, len(parts) + 1):
        rel = "/".join(parts[:depth])
        reason = builder_mod.ignore_reason(parts[depth - 1], depth < len(parts),
                                           patterns, rel)
        if reason:
            return reason
    return None


def _scan_exclusions(request: BuildRequest):
    """(kept_py, dropped_py, unmatched_patterns) for the project as it would be copied.

    Traversal is pruned only by the BUILT-IN rules, never by the operator's own
    patterns: a pattern must get a fair chance to match something even inside a
    folder that another pattern also excludes, or `data/*.csv` alongside `data/`
    would be reported as a typo.
    """
    from . import builder as builder_mod

    project = Path(request.project_dir)
    patterns = builder_mod.ignore_patterns_for(request)
    # `!foo` is a re-include; "it matched nothing" is not a meaningful complaint
    # about one, so we only ask the question of the patterns that DROP things.
    unmatched = {p: True for p in patterns
                 if p.strip() and not p.strip().startswith(("!", "#"))}

    kept_py: list[str] = []
    dropped_py: dict[str, str] = {}
    stack: list[tuple[Path, str, str | None]] = [(project, "", None)]
    while stack:
        directory, rel_dir, inherited = stack.pop()
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name
            rel = f"{rel_dir}/{name}" if rel_dir else name
            is_dir = child.is_dir()

            builtin = builder_mod.ignore_reason(name, is_dir, (), rel)
            for pattern in [p for p, still in unmatched.items() if still]:
                # The pattern matched this entry iff adding it CHANGES the verdict
                # the built-in rules alone would have given.
                if builder_mod.ignore_reason(name, is_dir, (pattern,), rel) != builtin:
                    unmatched[pattern] = False

            reason = inherited or builder_mod.ignore_reason(name, is_dir, patterns, rel)
            if is_dir:
                if builtin is None:          # never descend into .git / node_modules
                    stack.append((child, rel, reason))
            elif name.endswith(".py"):
                if reason:
                    dropped_py[rel] = reason
                else:
                    kept_py.append(rel)
    return kept_py, dropped_py, [p for p, still in unmatched.items() if still]


def exclusion_errors(request: BuildRequest) -> list[str]:
    """The exclusion mistakes that must never be delivered.

    One pattern (`data/*`, collapsed to `*`) once excluded every file in the
    project and the build reported 建立完成 — a package with no application code in
    it. Neither of these is a matter of taste: an entry script that is not in the
    package cannot start, and a package with no .py in it is not an app."""
    if not Path(request.project_dir).is_dir() or not Path(request.entrypoint).is_file():
        return []
    from . import builder as builder_mod

    patterns = builder_mod.ignore_patterns_for(request)
    if not patterns:
        return []

    errors: list[str] = []
    entry_reason = _exclusion_reason(request.project_dir, patterns, request.entrypoint)
    if entry_reason:
        errors.append(
            f"排除樣式會把入口檔本身排掉:{request.entrypoint.name}\n"
            f"  是這條排除掉的:{entry_reason}\n"
            "  交付包裡不會有這個檔案,App 一啟動就找不到入口。\n"
            "  請修掉 .provisionignore 或「進階設定 → 額外排除」裡的那條樣式。")

    kept_py, dropped_py, _unmatched = _scan_exclusions(request)
    if dropped_py and not kept_py:
        culprits = sorted(set(dropped_py.values()))
        sample = sorted(dropped_py)[:3]
        errors.append(
            "排除樣式會把專案裡「每一個」.py 檔都排掉,交付包裡不會有任何程式碼。\n"
            f"  是這些排除掉的:{'、'.join(culprits)}\n"
            f"  例如:{'、'.join(sample)}"
            + (f" …等 {len(dropped_py)} 個檔案" if len(dropped_py) > 3 else "") + "\n"
            "  最常見的原因:`data/*` 這種樣式以前會被收斂成 `*`,一條就掃掉整個專案。\n"
            "  請修掉 .provisionignore 或「進階設定 → 額外排除」裡的那條樣式。")
    return errors


def exclusion_warnings(request: BuildRequest) -> list[str]:
    """A pattern that matches NOTHING is now the only way a bad one can hide.

    The operator types `recordigs/*` (a typo for `recordings/*`), the GUI accepts
    it, the report says the exclusion is in force, and the 85 MB folder ships
    anyway. Nothing failed, so nothing was said."""
    if not Path(request.project_dir).is_dir():
        return []
    _kept, _dropped, unmatched = _scan_exclusions(request)
    return [f"排除樣式「{pattern}」在這個專案裡沒有比對到任何東西——是不是打錯了?"
            "(它不會排除任何檔案,對應的內容還是會被打包進去)"
            for pattern in unmatched]


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
                "  解法:在專案自己的環境裡執行 → pip freeze > requirements.lock.txt\n"
                + _PIP_FREEZE_CAVEAT]
    try:
        pins = normalize_lock(found.path.read_text("utf-8", errors="replace"))
    except LockfileError as exc:
        # normalize_lock says what is WRONG with the line and stops there. For the
        # two lines `pip freeze` itself emits — `-e .` and `pkg @ file:///…` — that
        # is a dead end we sent the operator into: we told them to run pip freeze
        # (right above, and in three other places), and then we refuse its output
        # with no way forward. Until normalize_lock drops those lines itself, at
        # least finish the sentence.
        return [str(exc) + _next_step_for_lock_error(found.path)]
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

        # An exclusion pattern that drops the entry script, or every .py in the
        # project, produces a package that cannot possibly start — and used to be
        # reported as 建立完成. Nothing downstream reads these patterns again.
        errors += exclusion_errors(request)

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

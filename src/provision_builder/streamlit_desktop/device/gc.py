"""Garbage collection: manual, dry-run by default (spec §11).

Keep-set = every slot of every app (current/previous/pending/candidate/LKG)
plus live leases, plus whatever runtime this very interpreter runs from.
Anything unresolvable makes GC refuse rather than guess — deleting a runtime
someone still needs is the one mistake this module exists to prevent.

Rules learned the hard way — the first two about the console, the rest about not
lying to the person reading it:

* Operator-facing text must survive **cp950** (the default code page of a zh-TW
  Windows console). A single U+26A0 in the summary made `print()` raise
  UnicodeEncodeError — no emoji, no box-drawing, no dingbats. Ever.
* Printing must never be able to prevent reclamation. The summary used to be
  logged BEFORE the delete loop, so that same UnicodeEncodeError meant the
  operator freed exactly zero bytes. Deletions run first now, and every write to
  the console goes through _emit(), which cannot raise.
* What --apply reports is what --apply DID (GcPlan.report(), past tense, measured
  from the trees that actually went away). The plan's 「可回收 N MB」 is a
  forecast; printing it after the fact told operators they had reclaimed space
  that rmtree had never managed to take.
* A tree that will not delete (the App is still open, antivirus has it, Explorer
  is sitting in it) is REPORTED. shutil.rmtree(ignore_errors=True) turned that
  into silence under a cheerful reclamation figure.
* "沒有可回收的項目" is only ever printed when it is true. GC runs under one of
  the runtimes it manages, and gc.bat picks whichever python.exe it finds last —
  which can be the orphan itself. Empty delete-lists then mean "the only thing to
  reclaim is the floor I am standing on", not "nothing to reclaim".
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # we run under the shared runtime; never mutate it

import shutil
from dataclasses import dataclass, field
from pathlib import Path

if __package__:
    from . import integrity, leases, paths as paths_mod, state as state_mod
    from .locks import LockTimeout, store_gc_lock
    from .runtime_store import RuntimeStore, ShellStore
else:
    import integrity
    import leases
    import paths as paths_mod
    import state as state_mod
    from locks import LockTimeout, store_gc_lock
    from runtime_store import RuntimeStore, ShellStore

STAGING_PREFIX = ".staging-"


class GcError(Exception):
    pass


def _emit(log, message: str) -> None:
    """Write to the console without ever letting the console kill the run.

    cp950 cannot encode every character Python can produce; a stray one used to
    abort GC before it deleted anything. Degrade the text, never the work.
    """
    try:
        log(message)
    except UnicodeEncodeError:
        try:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            log(message.encode(encoding, "replace").decode(encoding, "replace"))
        except Exception:  # noqa: BLE001 - the console is not worth a failed GC
            pass
    except OSError:
        pass


@dataclass
class GcPlan:
    keep_versions: dict = field(default_factory=dict)   # app_id -> set[version]
    keep_fingerprints: set = field(default_factory=set)
    keep_shells: set = field(default_factory=set)
    delete_versions: list = field(default_factory=list)  # (app_id, version, Path)
    delete_runtimes: list = field(default_factory=list)  # (fingerprint, Path)
    delete_shells: list = field(default_factory=list)    # (fingerprint, Path)
    delete_staging: list = field(default_factory=list)   # (what, Path)
    # The runtime GC itself is running from. It is only ever set when that runtime
    # is an ORPHAN (referenced runtimes are skipped before this branch) — i.e. it
    # is always something the operator could and would want to reclaim.
    self_hosted: str | None = None
    self_hosted_path: Path | None = None
    # A python.exe from a runtime that will still be there afterwards, so the
    # "run it with a different python" advice is a command, not a riddle.
    alternate_python: str | None = None
    root: Path | None = None
    # Filled in by run_gc(apply=True): what was ACTUALLY deleted, and what would
    # not go. Anything reported to the operator after --apply comes from here,
    # never from the plan — a plan is a promise, and the operator is owed the
    # outcome.
    applied: bool = False
    deleted: list = field(default_factory=list)          # (label, mb)
    failures: list = field(default_factory=list)         # str

    def _mb(self, path: Path) -> float:
        try:
            return sum(f.stat().st_size for f in Path(path).rglob("*")
                       if f.is_file()) / 1024 ** 2
        except OSError:
            return 0.0

    def is_empty(self) -> bool:
        return not (self.delete_versions or self.delete_runtimes
                    or self.delete_shells or self.delete_staging)

    def reclaimable_mb(self) -> float:
        return sum(self._mb(p) for _fp, p in self.delete_runtimes) \
            + sum(self._mb(p) for _fp, p in self.delete_shells) \
            + sum(self._mb(p) for _a, _v, p in self.delete_versions) \
            + sum(self._mb(p) for _w, p in self.delete_staging)

    def reclaimed_mb(self) -> float:
        return sum(mb for _label, mb in self.deleted)

    def items(self) -> list:
        """Everything to delete, as (label, path), in deletion order."""
        return ([(f"版本 {a}/{v}", p) for a, v, p in self.delete_versions]
                + [(f"runtime {fp}", p) for fp, p in self.delete_runtimes]
                + [(f"shell {fp}", p) for fp, p in self.delete_shells]
                + [(f"建置殘留 {w}", p) for w, p in self.delete_staging])

    # ── operator-facing text (plain, cp950-encodable: no emoji, no box-drawing) ──

    def self_hosted_note(self) -> str:
        """Why the orphan runtime we are EXECUTING FROM survived, and how to
        actually reclaim it.

        The old note was a footnote under 「沒有可回收的項目。」 — GC ran from the
        very runtime it should have deleted, truthfully found nothing else, and
        told the operator there was nothing to reclaim. They believed it, and the
        450 MB orphan stayed on the machine forever. The one documented entry
        point (tools\\gc.bat) picks `python.exe` from whichever runtime folder
        sorts last, so it lands on the orphan by pure luck of the fingerprint.
        """
        size = self._mb(self.self_hosted_path) if self.self_hosted_path else 0.0
        head = ("沒有其他可回收的項目,但是有一份沒人在用的 runtime 這次回收不掉:"
                if self.is_empty() else "另外有一份沒人在用的 runtime 這次回收不掉:")
        lines = [
            f"\n[注意] {head}",
            f"  runtime {self.self_hosted}({size:.0f} MB)沒有任何版本引用它,"
            f"但 GC 正是用它裡面的 python.exe 在執行,不能砍掉自己腳下的地板。",
        ]
        if self.alternate_python:
            lines += [
                "  要把它回收掉:改用「另一份」runtime 的 python.exe 重跑一次。",
                f"  在 {self.root or '程式資料夾'} 底下執行:",
                f"      {self.alternate_python} bootstrap\\gc.py --apply",
            ]
        else:
            lines += [
                "  這台機器上沒有第二份 runtime 可以改用。bootstrap\\gc.py 只用標準函式庫,",
                "  任何一個 Python 3 都跑得動它,例如:",
                "      C:\\Python313\\python.exe bootstrap\\gc.py --apply",
            ]
        return "\n".join(lines)

    def summary(self) -> str:
        """The PLAN (dry-run). Everything here is 「可刪 / 可回收」 — nothing has
        happened yet. After --apply, report() is what the operator gets."""
        lines = [f"保留 runtime:{sorted(self.keep_fingerprints)}",
                 f"保留 shell:{sorted(self.keep_shells)}"]
        lines += [f"可刪版本:{a}/{v}({self._mb(p):.0f} MB)" for a, v, p in self.delete_versions]
        lines += [f"可刪 runtime:{fp}({self._mb(p):.0f} MB)" for fp, p in self.delete_runtimes]
        lines += [f"可刪 shell:{fp}({self._mb(p):.0f} MB)" for fp, p in self.delete_shells]
        lines += [f"可刪建置殘留:{w}({self._mb(p):.0f} MB)" for w, p in self.delete_staging]
        if not self.is_empty():
            lines.append(f"可回收合計:{self.reclaimable_mb():.0f} MB")
        elif not self.self_hosted:
            # ONLY here is "nothing to reclaim" true. With self_hosted set, the
            # empty delete lists are not the absence of an orphan — they are the
            # orphan we are standing in.
            lines.append("沒有可回收的項目。")
        if self.self_hosted:
            lines.append(self.self_hosted_note())
        return "\n".join(lines)

    def report(self) -> str:
        """What --apply ACTUALLY did. Past tense, measured from the trees that
        really went away — never the plan's 「可回收」 figure, which is a forecast
        and was printed verbatim after the fact even when every rmtree had failed."""
        lines = [f"已刪除 {label}({mb:.0f} MB)" for label, mb in self.deleted]
        if self.deleted:
            lines.append(f"實際回收合計:{self.reclaimed_mb():.0f} MB")
        elif not self.failures and not self.self_hosted:
            lines.append("沒有可回收的項目,沒有刪除任何東西。")
        if self.failures:
            lines.append("下列項目刪不掉,空間「沒有」回收:")
            lines += [f"  · {problem}" for problem in self.failures]
            lines.append("  最常見的原因:App 還開著,或檔案總管/防毒正在讀那個資料夾。")
            lines.append("  請把 App 完全關掉(所有視窗),再重跑一次。")
        if self.self_hosted:
            lines.append(self.self_hosted_note())
        return "\n".join(lines)


def _fingerprints_of(paths: paths_mod.AppPaths, version: str) -> tuple[str, str | None]:
    """(runtime, shell) — anything unreadable aborts GC rather than guessing."""
    manifest = paths_mod.load_manifest(paths.version_dir(version))
    fingerprint = manifest.get("runtime_fingerprint")
    if not fingerprint:
        raise GcError(f"{paths.app_id}/{version} 的 manifest 缺 runtime_fingerprint,"
                      "無法安全計算 keep-set,GC 中止")
    return fingerprint, manifest.get("shell_fingerprint")


def _collect_staging(plan: GcPlan, label: str, parent: Path, *,
                     prefixed: bool = True) -> None:
    """Interrupted builds/downloads leave whole runtime trees behind (hundreds of
    MB each: a python-build-standalone + site-packages). They live under dot-names
    that every other scan here deliberately skips, so until now the one thing the
    operator ran GC to reclaim was the one thing GC could not see."""
    if not parent.is_dir():
        return
    try:
        children = sorted(parent.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir():
            continue
        if prefixed and not child.name.startswith(STAGING_PREFIX):
            continue
        plan.delete_staging.append((f"{label}/{child.name}", child))


def _alternate_python(runtimes: Path, *, exclude: str, keep: set) -> str | None:
    """A python.exe that will STILL be there after this GC — i.e. one belonging to
    a runtime some version actually references. Suggesting a doomed runtime's
    interpreter would hand the operator a command that stops working the moment
    they run it."""
    for fingerprint in sorted(keep):
        if fingerprint == exclude:
            continue
        if (runtimes / fingerprint / "python.exe").is_file():
            return f"deps\\runtimes\\{fingerprint}\\python.exe"
    return None


def collect_plan(root: Path) -> GcPlan:
    root = Path(root)
    plan = GcPlan(root=root)
    # This interpreter's own runtime is never deletable: rmtree'ing the tree we
    # execute from would leave a half-dead store (open-image deletes fail).
    own_prefix = Path(sys.prefix).resolve()

    for app_id in paths_mod.list_app_ids(root):
        paths = paths_mod.AppPaths(root, app_id)
        state = state_mod.StateStore(paths.state_dir).load()  # broken state → loud abort
        keep = {v for v in (state.current, state.previous, state.pending,
                            state.candidate, state.last_known_good) if v}
        for lease in leases.valid_leases(paths.data_dir / "leases"):
            if lease.get("version"):
                keep.add(lease["version"])
            if lease.get("runtime_fingerprint"):
                plan.keep_fingerprints.add(lease["runtime_fingerprint"])
        plan.keep_versions[app_id] = keep
        for version in keep:
            if paths.version_dir(version).is_dir():
                runtime_fp, shell_fp = _fingerprints_of(paths, version)
                plan.keep_fingerprints.add(runtime_fp)
                if shell_fp:
                    plan.keep_shells.add(shell_fp)

        if paths.versions_dir.is_dir():
            for child in sorted(paths.versions_dir.iterdir()):
                if not child.is_dir() or child.name in keep:
                    continue
                if child.name.startswith("."):
                    continue  # .staging-* — collected below, reported as such
                plan.delete_versions.append((app_id, child.name, child))
        # store_builder stages a version under versions/.staging-*; the updater
        # stages under apps/<app>/staging/<hex> (no dot prefix: the whole dir is
        # scratch space, so every child of it is reclaimable).
        _collect_staging(plan, f"{app_id}/versions", paths.versions_dir)
        _collect_staging(plan, f"{app_id}/staging", paths.staging_dir, prefixed=False)

    runtimes = RuntimeStore(root / "deps").runtimes
    if runtimes.is_dir():
        for child in sorted(runtimes.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in plan.keep_fingerprints:
                continue
            if own_prefix == child.resolve() or own_prefix in child.resolve().parents:
                # We are executing from inside this very runtime. Silently
                # skipping it means the operator runs GC, sees "nothing to do",
                # and the 450 MB orphan they came to delete stays forever.
                plan.self_hosted = child.name
                plan.self_hosted_path = child
                continue
            plan.delete_runtimes.append((child.name, child))
    if plan.self_hosted:
        plan.alternate_python = _alternate_python(
            runtimes, exclude=plan.self_hosted, keep=plan.keep_fingerprints)

    shells = ShellStore(root / "deps").shells
    if shells.is_dir():
        for child in sorted(shells.iterdir()):
            if child.is_dir() and not child.name.startswith(".") \
                    and child.name not in plan.keep_shells:
                plan.delete_shells.append((child.name, child))

    _collect_staging(plan, "deps/runtimes", runtimes)
    _collect_staging(plan, "deps/shells", shells)
    return plan


def _delete_tree(path: Path) -> list[str]:
    """Delete a tree; return what stopped us, if anything.

    This used to be shutil.rmtree(ignore_errors=True), which turns "the App is
    still running / an antivirus has the folder open / Explorer is sitting in it"
    into silence — and GC then printed 「可回收 480 MB」 with all 480 MB still on
    the disk. A GC that cannot delete something must SAY so; the operator can
    close the app and run it again, but only if they are told.
    """
    try:
        integrity.remove_complete(path)  # first: make it invisible (fail closed)
    except OSError as exc:
        return [f"{path}:連 .complete 都刪不掉({exc})"]
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return [f"{path}({exc})"]
    if path.exists():
        return [f"{path}:資料夾還在"]
    return []


def run_gc(root: Path, *, apply: bool = False, log=print) -> GcPlan:
    root = Path(root)
    # The updater takes this very lock around staging (updater._STAGE_LOCK_TIMEOUT),
    # so a runtime being downloaded right now cannot be swept away half-written.
    with store_gc_lock(root / "deps"):
        plan = collect_plan(root)        # scan INSIDE the lock (spec §11)
        if not apply:
            _emit(log, plan.summary())
            _emit(log, "(dry-run;加 --apply 才會真的刪除)")
            return plan

        # Delete FIRST, talk afterwards. A console that cannot encode part of the
        # summary must not be able to cost the operator the disk space they came
        # for — that is precisely what happened when the summary was printed here.
        plan.applied = True
        for label, path in plan.items():
            size = plan._mb(path)        # measure it while it still exists
            _emit(log, f"刪除 {label} …")
            problems = _delete_tree(path)
            if problems:
                plan.failures.extend(problems)
            else:
                plan.deleted.append((label, size))
        # Past tense, from what actually happened — not the forecast we printed
        # before touching anything.
        _emit(log, plan.report())
    return plan


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="回收未被任何槽引用的版本與 runtime")
    parser.add_argument("--apply", action="store_true", help="真的刪除(預設只列出)")
    args = parser.parse_args()
    try:
        result = run_gc(Path(__file__).resolve().parents[1], apply=args.apply)
        # Trees we could not remove already said so on the console (plan.report()).
        # Exiting 0 on top of that would tell gc.bat — and any script wrapping it
        # — that the space came back.
        if result.failures:
            raise SystemExit(2)
    except LockTimeout:
        print("\n[gc][ERROR] 目前有更新正在下載或安裝(store 鎖被佔用),這次沒有刪除任何東西。\n"
              "  請等它完成後再重跑一次。", file=sys.stderr)
        raise SystemExit(2) from None
    except (GcError, state_mod.StateError, paths_mod.LayoutError,
            UnicodeEncodeError) as exc:
        # A traceback tells a factory IT nothing. Say what is wrong and that
        # NOTHING was deleted — GC aborts whole rather than guess.
        # UnicodeEncodeError is in this list because the console, not the store,
        # is the thing that broke: cp950 cannot print every character. _emit()
        # should have absorbed it, so reaching here means a message we do not
        # control leaked out — say so instead of dumping a traceback.
        print(f"\n[gc][ERROR] {exc}\n"
              "  為了安全,這次沒有刪除任何東西。\n"
              "  請先修好上面提到的問題,再重跑一次。", file=sys.stderr)
        raise SystemExit(2) from None
    except OSError as exc:
        print(f"\n[gc][ERROR] 磁碟操作失敗:{exc}\n  沒有刪除任何東西。", file=sys.stderr)
        raise SystemExit(2) from None

"""Garbage collection: manual, dry-run by default (spec §11).

Keep-set = every slot of every app (current/previous/pending/candidate/LKG)
plus live leases, plus whatever runtime this very interpreter runs from.
Anything unresolvable makes GC refuse rather than guess — deleting a runtime
someone still needs is the one mistake this module exists to prevent.

Two rules learned the hard way, both about the console:

* Operator-facing text must survive **cp950** (the default code page of a zh-TW
  Windows console). A single U+26A0 in the summary made `print()` raise
  UnicodeEncodeError — no emoji, no box-drawing, no dingbats. Ever.
* Printing must never be able to prevent reclamation. The summary used to be
  logged BEFORE the delete loop, so that same UnicodeEncodeError meant the
  operator freed exactly zero bytes. Deletions run first now, and every write to
  the console goes through _emit(), which cannot raise.
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
    self_hosted: str | None = None      # the runtime GC itself is running from

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

    def summary(self) -> str:
        """Plain text only — this string reaches a cp950 console."""
        lines = [f"保留 runtime:{sorted(self.keep_fingerprints)}",
                 f"保留 shell:{sorted(self.keep_shells)}"]
        lines += [f"可刪版本:{a}/{v}({self._mb(p):.0f} MB)" for a, v, p in self.delete_versions]
        lines += [f"可刪 runtime:{fp}({self._mb(p):.0f} MB)" for fp, p in self.delete_runtimes]
        lines += [f"可刪 shell:{fp}({self._mb(p):.0f} MB)" for fp, p in self.delete_shells]
        lines += [f"可刪建置殘留:{w}({self._mb(p):.0f} MB)" for w, p in self.delete_staging]
        if not self.is_empty():
            lines.append(f"可回收合計:{self.reclaimable_mb():.0f} MB")
        else:
            lines.append("沒有可回收的項目。")
        if self.self_hosted:
            lines.append(
                f"\n[注意] GC 正用 {self.self_hosted} 這份 runtime 的 python 在執行,"
                "\n  所以「它自己」即使沒被引用也不會被刪除。"
                "\n  若要回收它,請改用另一份 runtime 的 python.exe 重跑一次。")
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


def collect_plan(root: Path) -> GcPlan:
    root = Path(root)
    plan = GcPlan()
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
                continue
            plan.delete_runtimes.append((child.name, child))

    shells = ShellStore(root / "deps").shells
    if shells.is_dir():
        for child in sorted(shells.iterdir()):
            if child.is_dir() and not child.name.startswith(".") \
                    and child.name not in plan.keep_shells:
                plan.delete_shells.append((child.name, child))

    _collect_staging(plan, "deps/runtimes", runtimes)
    _collect_staging(plan, "deps/shells", shells)
    return plan


def _delete_tree(path: Path) -> None:
    integrity.remove_complete(path)      # first: make it invisible (fail closed)
    shutil.rmtree(path, ignore_errors=True)


def run_gc(root: Path, *, apply: bool = False, log=print) -> GcPlan:
    root = Path(root)
    # The updater takes this very lock around staging (updater._STAGE_LOCK_TIMEOUT),
    # so a runtime being downloaded right now cannot be swept away half-written.
    with store_gc_lock(root / "deps"):
        plan = collect_plan(root)        # scan INSIDE the lock (spec §11)
        # Build the text while the trees still exist (it reports their sizes),
        # but do not put it on the console yet.
        summary = plan.summary()
        if not apply:
            _emit(log, summary)
            _emit(log, "(dry-run;加 --apply 才會真的刪除)")
            return plan

        # Delete FIRST, talk afterwards. A console that cannot encode part of the
        # summary must not be able to cost the operator the disk space they came
        # for — that is precisely what happened when the summary was printed here.
        for app_id, version, path in plan.delete_versions:
            _emit(log, f"刪除版本 {app_id}/{version}")
            _delete_tree(path)
        for fingerprint, path in plan.delete_runtimes:
            _emit(log, f"刪除 runtime {fingerprint}")
            _delete_tree(path)
        for fingerprint, path in plan.delete_shells:
            _emit(log, f"刪除 shell {fingerprint}")
            _delete_tree(path)
        for what, path in plan.delete_staging:
            _emit(log, f"刪除建置殘留 {what}")
            _delete_tree(path)
        _emit(log, summary)
    return plan


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="回收未被任何槽引用的版本與 runtime")
    parser.add_argument("--apply", action="store_true", help="真的刪除(預設只列出)")
    args = parser.parse_args()
    try:
        run_gc(Path(__file__).resolve().parents[1], apply=args.apply)
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

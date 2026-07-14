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
* Every failure mode gets its OWN exit code. Four different disasters used to exit
  2, so "I deleted 3 of the 5 trees, 2 are still open in the App" and "I could not
  even take the store lock and did nothing at all" were indistinguishable to the
  caller — and gc.bat then printed 「沒有刪掉任何東西」 over a run that had just
  reclaimed 400 MB, and blamed the store lock for it.
* An --apply run that deleted NOTHING is not an --apply run that deleted
  EVERYTHING. Both used to exit 0, and gc.bat's exit-0 branch prints 「回收完成。
  上面列出的項目都已經刪掉了。」 — over a run that listed nothing, deleted nothing
  and freed exactly 0 bytes. "上面列出的項目" was the empty set. A plan is a
  promise, and the operator is owed the outcome: EXIT_EMPTY_PLAN says the store was
  already clean, and says it without calling a clean store a failure.


GC EXIT-CODE CONTRACT (gc.py <-> tools\\gc.bat and anything else wrapping it)
============================================================================
These numbers are a CONTRACT with store_builder.GC_EXIT_* (which generates the bat
that reads them). Change one side and the console starts telling the other side's
story — which is the very bug this table exists to end.

    0   THERE IS SOMETHING TO DO, AND IT WENT. Either a DRY RUN whose plan is not
        empty (gc.bat then asks 「真的要刪除嗎?」), or an --apply that deleted every
        tree in the plan. Only this code may be answered with 「回收完成…上面列出的
        項目都已經刪掉了」, because only here was something listed and something went.
    2   PARTIAL. Some trees went, some would not. The survivors' space is NOT
        reclaimed; they are listed by name with the reason (檔案使用中 → close the
        App and run it again). Saying 「沒有刪掉任何東西」 here is a lie — and it
        was printed over runs that had just reclaimed 400 MB.
    3   NOTHING DELETED. GC tried, or refused to try, and zero bytes came back —
        and the console says WHICH: GC refused before touching anything (a broken
        state.json, a manifest with no runtime_fingerprint, a disk error in the
        scan), or it tried every tree and every single one refused (the App is
        open, antivirus/Explorer is holding the folder). Something IS wrong here.
    4   STORE LOCKED. An update is downloading or installing right now, so GC never
        even scanned. Nothing was deleted; nothing is wrong. Try again later.
    6   EMPTY PLAN — there is nothing to reclaim. Returned by the DRY RUN and by
        --apply alike, and it is not a failure (gc.bat's :empty branch exits 0).
        On the dry run it is what stops the bat asking 「以上列出的項目要真的刪除
        嗎?」 over a blank list; on --apply it is what stops it answering 「都已經
        刪掉了」. It is NOT 3: an already-clean store is not a failed GC, and 3's
        message is 「回收失敗」.

Before this, all four failures exited 2 — and 0 covered both "deleted every tree"
and "there was nothing to delete", which is how 「上面列出的項目都已經刪掉了」 came
to be printed over a run that listed nothing and freed 0 bytes.

WHAT ACTUALLY FILLS THE DISK IS NOT THE VERSIONS
================================================
GC used to look at version dirs, runtimes, shells and leases — and nothing else.
But nothing in this system ever rotated a log: every launch writes a
launcher-*.log AND a streamlit-*.log AND a bootstrap-*.log, forever, and
PYTHONPYCACHEPREFIX points every .pyc the app compiles at data/cache/pycache. On
a machine that has been running for months, THAT is where the C: drive went — and
it was the one thing GC could not see. So:

  * apps/<app>/data/logs/  is scanned, and every log except the newest
    LOG_KEEP_RECENT of each family (launcher-/streamlit-/bootstrap-) is offered
    for reclamation, reported SEPARATELY from versions and runtimes.
  * apps/<app>/data/cache/ (pycache) is offered too: it is regenerated on the
    next launch and costs nothing but a slightly slower start.
  * rotate_logs() is the retention half — call it wherever the logs are created
    (bootstrap.py, right after ensure_data_dirs()) so this stops happening.

AND: AN EMPTY PLAN IS NOT AN ANSWER TO 「C 槽快滿了」
====================================================
「沒有可回收的項目」 is true and useless to somebody whose disk is full. Every run
now also reports WHERE THE SPACE WENT: shutil.disk_usage() (free space on the
drive), the size of this tree, and the biggest consumers inside it — including how
much of it is logs. If the store is not the culprit, GC says so out loud
(「這棵樹只佔 x MB;C 槽的空間不是被它吃掉的」), which is the answer they came for
even though it reclaims nothing.

AND: A SIZE THAT COULD NOT BE MEASURED IS NOT ZERO
=================================================
GcPlan._mb() wrapped the whole rglob in try/except and returned 0.0 — so ONE file
held open by a running App made a 450 MB runtime report as 「0 MB」, and the
operator concluded there was nothing to gain. Measurement now walks per entry,
counts what it could not read (Measured.unreadable), and says 「至少 N MB」 instead
of pretending a runtime is empty.

WHAT A GUI SHOULD READ (never re-derive any of this from the plan's forecast):
    plan.applied            did --apply actually run?
    plan.deleted            [(label, mb)] — what ACTUALLY went away
    plan.reclaimed_mb()     MEASURED total. reclaimable_mb() is a FORECAST: it is
                            only ever valid before --apply.
    plan.survivors          [GcSurvivor] — what stayed, .reason why, .in_use
                            (→ .hint(): 關掉 App 再跑一次), .label, .path
    plan.failures           the same survivors, pre-rendered as console lines
    plan.nothing_to_reclaim()   True (== is_empty()) → say 「沒有可回收的項目」,
                            never 「已刪除」 and never a reclaimed total
    plan.headline()         one honest cp950-safe line, safe to show verbatim
    plan.exit_code()        the table above

    …and, for the person whose C: drive is full — valid even when the plan is EMPTY:
    plan.disk               DiskSpace: .total_mb .used_mb .free_mb .free_pct
                            .nearly_full() .path ("C:") .known (False = unreadable)
    plan.disk_after         the same, re-measured after --apply (None on a dry run)
    plan.store_mb()         how much of the disk THIS TREE is holding
    plan.consumers          [Consumer] — .label .kind .mb .reclaimable_mb .partial
                            kinds: versions|logs|cache|data|staging|runtime|shell|other
    plan.biggest(n)         the same, largest first: which runtime, how big, how
                            much of it is logs
    plan.delete_logs        [GcGroup] — old rotated logs (.count .mb .paths .root)
    plan.delete_caches      [GcGroup] — data/cache (regenerable)
    plan.logs_mb() / plan.cache_mb()        what the plan would reclaim from each
    plan.store_is_the_problem()  False → 「C 槽的空間不是被這棵樹吃掉的」
    plan.unmeasured         [(path, why)] — files we could NOT size (a running App
                            holds them). Sizes near these are 「至少」, never exact.
"""

from __future__ import annotations

import errno
import sys

sys.dont_write_bytecode = True  # we run under the shared runtime; never mutate it

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

if __package__:
    from . import integrity, leases, paths as paths_mod, state as state_mod
    from .identifiers import IdentifierError
    from .locks import LockTimeout, store_gc_lock
    from .runtime_store import RuntimeStore, ShellStore
else:
    import integrity
    import leases
    import paths as paths_mod
    import state as state_mod
    from identifiers import IdentifierError
    from locks import LockTimeout, store_gc_lock
    from runtime_store import RuntimeStore, ShellStore

STAGING_PREFIX = ".staging-"

# ── exit codes (see the module docstring; these MUST equal store_builder.GC_EXIT_*)

EXIT_OK = 0
EXIT_PARTIAL = 2            # some trees deleted, some still in use
EXIT_NOTHING_DELETED = 3    # zero bytes reclaimed: refused up front, or every tree refused
EXIT_ABORTED = EXIT_NOTHING_DELETED   # refusing IS a nothing-deleted run; the text says which
EXIT_STORE_LOCKED = 4       # never even scanned: an update holds the store lock
# THE PLAN IS EMPTY: there is nothing to reclaim. Returned by BOTH the dry run and
# --apply, and it is not a failure (tools\gc.bat's :empty branch exits 0).
#
# It used to be EXIT_OK — the same code as "deleted all 5 trees, freed 480 MB" — so
# the bat asked 「以上列出的項目要真的刪除嗎?」 over a blank list and then answered
# itself with 「回收完成。上面列出的項目都已經刪掉了。」 Nothing was listed, nothing
# was deleted, and the operator went looking for space that was never freed.
#
# Not EXIT_NOTHING_DELETED either: that one means something WENT WRONG (every tree
# refused, or GC refused up front) and its message is 「回收失敗」. An already-clean
# store is not a failed GC.
#
# The NAME and the VALUE are the contract: store_builder reads them as
# GC_EXIT_EMPTY = getattr(gc_mod, "EXIT_EMPTY_PLAN", 6) and bakes the number into
# every tools\gc.bat and tools\admin-*.bat it writes.
EXIT_EMPTY_PLAN = 6
EXIT_NOTHING_TO_RECLAIM = EXIT_EMPTY_PLAN   # what it MEANS, for anyone reading a caller

# Why a version is in the keep-set. The operator's first question when GC reclaims
# less than they expected is "why is THAT one still here?", and until now the
# keep-set was computed, obeyed, and never once printed.
KEEP_LABELS = (
    ("current", "目前版本"),
    ("previous", "上一版(退版目標)"),
    ("pending", "待套用的更新"),
    ("candidate", "首次啟動還沒通過驗證(candidate)"),
    ("last_known_good", "最後可用版本(LKG,退版目標)"),
)
KEEP_LEASE = "正在執行中(lease)"


IN_USE_HINT = "檔案使用中:請把 App 完全關掉(所有視窗),再重跑一次回收。"
OTHER_HINT = "請先排除上面的錯誤,再重跑一次回收。"

# ── log retention (the reason the disk filled up in the first place) ─────────
#
# Nothing rotated these, ever. Every launch appends one more launcher-*.log, one
# more streamlit-*.log and one more bootstrap-*.log to apps/<app>/data/logs, and
# they are all still there months later. Keep the newest few of each family — that
# is every log anyone ever reads — and let GC (and rotate_logs()) reclaim the rest.
LOG_KEEP_RECENT = 10          # per family: launcher-*, streamlit-*, bootstrap-*
# A log written seconds ago belongs to a session that is probably still running.
# Never touch it, whatever the count says: keeping too much is a wasted MB, and
# deleting the log of a live session is a support call nobody can answer.
LOG_FRESH_SECONDS = 3600.0
LOG_SUFFIX = ".log"

# How many unreadable paths we keep as examples. The COUNT is always exact; the
# list is a sample, because a locked runtime can produce thousands.
_UNMEASURED_SAMPLE = 20

# The disk is "nearly full" at either of these — whichever bites first. 10% of a
# 2 TB disk is 200 GB (not full at all), and 1 GB free on a 128 GB SSD is.
DISK_LOW_PERCENT = 10.0
DISK_LOW_MB = 1024.0
# Below this share of the disk's USED space, this tree cannot be why C: is full,
# and saying so is the most useful sentence GC can print on an already-clean store.
STORE_BLAME_SHARE = 0.10


class GcError(Exception):
    pass


def _in_use(exc: OSError) -> bool:
    """Is this the one failure the operator can actually fix by closing the App?

    Everything Windows says when a handle is still open on the tree: ACCESS_DENIED
    (5), SHARING_VIOLATION (32), LOCK_VIOLATION (33). Telling the operator to close
    the App for a disk error would be as useless as telling them nothing.
    """
    winerror = getattr(exc, "winerror", None)
    return bool(isinstance(exc, PermissionError) or winerror in (5, 32, 33)
                or exc.errno in (errno.EACCES, errno.EPERM, errno.EBUSY))


def _why(exc: OSError) -> str:
    """The operator-actionable half of an OSError, in the terms of the machine."""
    if _in_use(exc):
        return f"檔案使用中或沒有權限({exc})"
    return str(exc)


@dataclass
class GcSurvivor:
    """A tree --apply could NOT delete.

    The GUI needs the parts, not a sentence: WHAT it was (label), WHERE it is
    (path), WHY it stayed (reason) and whether that reason is the fixable one
    (in_use → 關掉 App 再跑一次). This used to be a bare f-string in a list, so the
    only way to show it was to print it, and the only way to act on it was to read
    it.
    """
    label: str
    path: str
    reason: str
    in_use: bool = False

    def hint(self) -> str:
        return IN_USE_HINT if self.in_use else OTHER_HINT

    def line(self) -> str:
        """The console form. The path leads, because that is what the operator has
        to go and close."""
        return f"{self.path}:{self.reason}"


@dataclass
class Measured:
    """A size that knows what it could NOT see.

    The old _mb() wrapped the entire directory walk in one try/except and returned
    0.0 on any error — so a single file held open by a running App (antivirus,
    Explorer, the App itself) made a 450 MB runtime report as 「0 MB」. The operator
    read 「可回收 0 MB」, concluded there was nothing to gain, and stopped. A number
    we could not measure is not zero: it is 「至少 N MB」, plus a count of what we
    could not read.
    """
    mb: float = 0.0
    files: int = 0
    unreadable: int = 0          # files/dirs we could not stat or list

    @property
    def partial(self) -> bool:
        return self.unreadable > 0

    def add(self, other: "Measured") -> "Measured":
        self.mb += other.mb
        self.files += other.files
        self.unreadable += other.unreadable
        return self

    def text(self) -> str:
        if self.partial:
            return (f"至少 {self.mb:,.0f} MB"
                    f"(有 {self.unreadable} 個檔案量不到大小,實際可能更多)")
        return f"{self.mb:,.0f} MB"


def _entry_size(entry) -> int:
    """The ONE place a file's size is read.

    A single function so that "a file a running App has open" is a thing that can
    be tested, instead of a thing we hope we handled.
    """
    return entry.stat(follow_symlinks=False).st_size


@dataclass
class DiskSpace:
    """How full the drive actually is. Nobody in this repo had ever asked.

    The operator did not run GC because they wanted a tidy store; they ran it
    because C: is full. An answer that never mentions the disk is not an answer.
    """
    path: str = ""               # "C:" — for a GUI
    total_mb: float = 0.0
    used_mb: float = 0.0
    free_mb: float = 0.0
    error: str | None = None     # the disk itself would not answer

    @property
    def known(self) -> bool:
        return self.error is None and self.total_mb > 0

    @property
    def label(self) -> str:
        """「C 槽」 — what the person on the phone calls it. (self.path is "C:",
        which reads as 「C::」 the moment you put a colon after it.)"""
        if len(self.path) == 2 and self.path[1] == ":" and self.path[0].isalpha():
            return f"{self.path[0].upper()} 槽"
        return self.path or "這個磁碟"

    @property
    def free_pct(self) -> float:
        return (100.0 * self.free_mb / self.total_mb) if self.total_mb else 0.0

    def nearly_full(self) -> bool:
        return self.known and (self.free_pct < DISK_LOW_PERCENT
                               or self.free_mb < DISK_LOW_MB)

    def line(self) -> str:
        if not self.known:
            return f"磁碟空間讀不到({self.error})"
        verdict = "快滿了" if self.nearly_full() else "還夠用"
        return (f"{self.label}:總共 {self.total_mb:,.0f} MB、"
                f"用掉 {self.used_mb:,.0f} MB、可用 {self.free_mb:,.0f} MB"
                f"(剩 {self.free_pct:.0f}%,{verdict})")


def disk_space(path: Path) -> DiskSpace:
    """shutil.disk_usage() of the volume `path` lives on — zero uses of it in this
    repo before now, on a system whose whole job is disk space."""
    path = Path(path)
    try:
        usage = shutil.disk_usage(str(path))
    except OSError as exc:
        return DiskSpace(path=str(path), error=str(exc))
    try:
        drive = os.path.splitdrive(str(path.resolve()))[0]
    except OSError:
        drive = ""
    return DiskSpace(path=drive or str(path),
                     total_mb=usage.total / 1024 ** 2,
                     used_mb=usage.used / 1024 ** 2,
                     free_mb=usage.free / 1024 ** 2)


@dataclass
class Consumer:
    """One line of 「你的磁碟到底被誰吃掉了」.

    Reported for EVERY run, including the ones with nothing to reclaim — that is
    the whole point: an operator with a full C: drive needs to know where the space
    went even (especially) when GC cannot give any of it back.
    """
    label: str
    kind: str                    # versions|logs|cache|data|staging|runtime|shell|other
    mb: float = 0.0
    reclaimable_mb: float = 0.0  # how much of it THIS run could take back
    partial: bool = False        # some of it could not be measured
    path: str | None = None

    def line(self) -> str:
        size = f"至少 {self.mb:,.0f} MB" if self.partial else f"{self.mb:,.0f} MB"
        if self.reclaimable_mb >= 1:
            return f"{self.label}:{size}(其中 {self.reclaimable_mb:,.0f} MB 這次可以回收)"
        return f"{self.label}:{size}"


@dataclass
class GcGroup:
    """Hundreds of small files that are ONE decision for the operator.

    400 rotated logs are not 400 choices; they are 「舊記錄檔」. The group keeps
    every path (so a partial failure names exactly what stayed) and prints as a
    single line (so the console stays readable).
    """
    kind: str                    # "logs" | "cache"
    app_id: str
    label: str
    root: Path
    paths: list = field(default_factory=list)
    mb: float = 0.0
    partial: bool = False

    @property
    def count(self) -> int:
        return len(self.paths)

    def line(self) -> str:
        size = f"至少 {self.mb:,.0f} MB" if self.partial else f"{self.mb:,.0f} MB"
        return f"{self.label}:{self.count} 個({size})"


# ── log retention ────────────────────────────────────────────────────────────

def _log_family(name: str) -> str:
    """launcher-20260712-101500.log -> "launcher". The family is what we keep N of."""
    return name.split("-", 1)[0].lower()


def stale_logs(log_dir: Path, *, keep: int = LOG_KEEP_RECENT,
               fresh_seconds: float = LOG_FRESH_SECONDS) -> list:
    """Every log EXCEPT the newest `keep` of each family and anything just written.

    Nobody has ever read the 300th-newest launcher log. Everybody needs the last
    couple, and the ones from a session that is running right now — so those are
    exactly what survives.
    """
    try:
        entries = [e for e in os.scandir(Path(log_dir)) if e.is_file()]
    except OSError:
        return []                    # no logs dir (or unreadable): nothing to do
    now = time.time()
    families: dict = {}
    for entry in entries:
        if not entry.name.lower().endswith(LOG_SUFFIX):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue                 # cannot judge its age -> never delete it
        if now - mtime < fresh_seconds:
            continue                 # a live session is writing it
        families.setdefault(_log_family(entry.name), []).append((mtime, Path(entry.path)))
    doomed: list = []
    for items in families.values():
        items.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
        doomed += [path for _mtime, path in items[keep:]]
    return sorted(doomed)


def rotate_logs(log_dir: Path, *, keep: int = LOG_KEEP_RECENT) -> list:
    """RETENTION. Delete all but the newest `keep` logs of each family; return what
    went. Best-effort and silent: a log the App still has open is simply left for
    next time.

    THIS IS THE FIX FOR THE ROOT CAUSE, and it belongs wherever the logs are
    CREATED — one call, right after the log dir is ensured (bootstrap.py's
    _setup_logging / start_app, which runs on every single launch). GC can only
    ever mop up afterwards; this is what stops the puddle.
    """
    removed: list = []
    for path in stale_logs(log_dir, keep=keep):
        try:
            os.remove(path)
        except OSError:
            continue                 # in use / no permission: next launch gets it
        removed.append(path)
    return removed


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
    # WHY each kept version is kept: app_id -> {version: [reason, …]}. Without this
    # the keep-set is a silent verdict, and the operator's only way to find out why
    # 3 GB did not come back is to read gc.py.
    keep_reasons: dict = field(default_factory=dict)
    keep_fingerprints: set = field(default_factory=set)
    keep_shells: set = field(default_factory=set)
    delete_versions: list = field(default_factory=list)  # (app_id, version, Path)
    delete_runtimes: list = field(default_factory=list)  # (fingerprint, Path)
    delete_shells: list = field(default_factory=list)    # (fingerprint, Path)
    delete_staging: list = field(default_factory=list)   # (what, Path)
    # What ACTUALLY fills a machine that has been running for months, and what GC
    # could not see at all until now: one launcher-*.log + one streamlit-*.log +
    # one bootstrap-*.log per launch, never rotated, plus every .pyc the app ever
    # compiled (PYTHONPYCACHEPREFIX -> data/cache/pycache).
    delete_logs: list = field(default_factory=list)      # [GcGroup] kind="logs"
    delete_caches: list = field(default_factory=list)    # [GcGroup] kind="cache"
    # Scope. apps_considered is what --app selected (default: everything); the rest
    # of apps_all is scanned for the KEEP-set (a shared runtime belongs to the
    # machine, not to one app) but nothing under it is deleted.
    apps_all: list = field(default_factory=list)
    apps_considered: list = field(default_factory=list)
    skipped_versions: list = field(default_factory=list)  # (app_id, version) - other app
    # Version trees of a NON-selected app whose manifest we could not read, so we
    # cannot know which shared runtime they pin. They survive this run, so we must
    # not delete any shared runtime either: "refuse rather than guess".
    unreadable: list = field(default_factory=list)        # (label, why)
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
    deleted: list = field(default_factory=list)          # (label, mb) — MEASURED
    survivors: list = field(default_factory=list)        # GcSurvivor
    # WHERE THE SPACE WENT — filled in by collect_plan() on EVERY run, valid even
    # when there is nothing to reclaim. An operator with a full C: drive is owed an
    # answer, and 「沒有可回收的項目」 is not one.
    disk: DiskSpace = field(default_factory=DiskSpace)
    disk_after: DiskSpace | None = None                  # re-measured after --apply
    consumers: list = field(default_factory=list)        # [Consumer], the breakdown
    # Files we could NOT size (a running App holds them open). Every total near one
    # of these is 「至少」, never exact — and NEVER 0 MB, which is what the old
    # blanket try/except reported for an entire 450 MB runtime.
    unmeasured: list = field(default_factory=list)       # sample of (path, why)
    unmeasured_count: int = 0                            # …the exact count
    _sizes: dict = field(default_factory=dict, repr=False)   # path -> Measured

    @property
    def failures(self) -> list:
        """The survivors, pre-rendered for a console. Kept as a view over the one
        source of truth: two lists that could disagree about whether 480 MB came
        back is exactly the bug this module exists to not have."""
        return [survivor.line() for survivor in self.survivors]

    # ── measurement ──────────────────────────────────────────────────────────
    #
    # The old version was `try: sum(...rglob...) except OSError: return 0.0`. One
    # unreadable file — an antivirus scan, Explorer sitting in the folder, the App
    # itself still running — and an entire 450 MB runtime reported as 0 MB. The
    # operator was told there was nothing to reclaim, and believed it.

    def measure(self, path: Path) -> Measured:
        """Size a tree, reporting what could not be read instead of calling it 0."""
        key = str(path)
        found = self._sizes.get(key)
        if found is None:
            found = self._walk(Path(path))
            self._sizes[key] = found
        return found

    def _walk(self, path: Path) -> Measured:
        result = Measured()
        if path.is_file():
            try:
                result.mb = path.stat().st_size / 1024 ** 2
                result.files = 1
            except OSError as exc:
                result.unreadable = 1
                self._note_unreadable(path, exc)
            return result
        total = 0
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except FileNotFoundError:
                continue                      # not there = genuinely 0, not unknown
            except OSError as exc:
                result.unreadable += 1
                self._note_unreadable(current, exc)
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    total += _entry_size(entry)
                    result.files += 1
                except OSError as exc:
                    # THE line this whole class exists for: one locked file costs
                    # us one file, not the entire runtime.
                    result.unreadable += 1
                    self._note_unreadable(Path(entry.path), exc)
        result.mb = total / 1024 ** 2
        return result

    def _note_unreadable(self, path: Path, exc: OSError) -> None:
        self.unmeasured_count += 1
        if len(self.unmeasured) < _UNMEASURED_SAMPLE:
            self.unmeasured.append((str(path), _why(exc)))

    def measurement_is_partial(self) -> bool:
        """True when at least one file could not be sized: every 「MB」 we print is
        a floor, not a total."""
        return self.unmeasured_count > 0

    def refresh_usage(self, root: Path) -> None:
        """Re-measure the tree AFTER --apply.

        The breakdown collect_plan() took is a snapshot of a tree that no longer
        exists: printing 「記錄檔 33 MB(其中 31 MB 這次可以回收)」 in the report of
        the run that just reclaimed them is the same past-tense forecast that made
        operators believe they had freed space rmtree never took. Measure again,
        and quote no forecast at all.
        """
        self._sizes.clear()
        self.consumers = []
        self.unmeasured = []
        self.unmeasured_count = 0
        _measure_store(self, Path(root), forecast=False)

    def _mb(self, path: Path) -> float:
        return self.measure(path).mb

    # ── the plan ─────────────────────────────────────────────────────────────

    def groups(self) -> list:
        """The bulk items (old logs, cache), which are deleted file by file but
        decided — and reported — as one thing each."""
        return list(self.delete_logs) + list(self.delete_caches)

    def is_empty(self) -> bool:
        return not (self.delete_versions or self.delete_runtimes
                    or self.delete_shells or self.delete_staging
                    or self.delete_logs or self.delete_caches)

    def logs_mb(self) -> float:
        """How much of the plan is old logs — the number that explains a disk that
        filled up 'by itself'."""
        return sum(group.mb for group in self.delete_logs)

    def cache_mb(self) -> float:
        return sum(group.mb for group in self.delete_caches)

    def reclaimable_mb(self) -> float:
        return sum(self._mb(p) for _fp, p in self.delete_runtimes) \
            + sum(self._mb(p) for _fp, p in self.delete_shells) \
            + sum(self._mb(p) for _a, _v, p in self.delete_versions) \
            + sum(self._mb(p) for _w, p in self.delete_staging) \
            + self.logs_mb() + self.cache_mb()

    def reclaimed_mb(self) -> float:
        return sum(mb for _label, mb in self.deleted)

    def items(self) -> list:
        """Every TREE to delete, as (label, path), in deletion order. The bulk file
        groups (logs, cache) are groups(), not items(): 400 rotated logs must not
        become 400 console lines and 400 entries in `deleted`."""
        return ([(f"版本 {a}/{v}", p) for a, v, p in self.delete_versions]
                + [(f"runtime {fp}", p) for fp, p in self.delete_runtimes]
                + [(f"shell {fp}", p) for fp, p in self.delete_shells]
                + [(f"建置殘留 {w}", p) for w, p in self.delete_staging])

    def item_count(self) -> int:
        return len(self.items()) + len(self.groups())

    def nothing_to_reclaim(self) -> bool:
        """There is nothing to delete — before --apply and after it alike. NOT a
        failure, and NOT a reclaim: the disk is exactly as full as it was."""
        return self.is_empty()

    # ── where the space went (true even when nothing can be reclaimed) ───────

    def store_mb(self) -> float:
        return sum(consumer.mb for consumer in self.consumers)

    def biggest(self, limit: int = 5) -> list:
        """The biggest things in this tree, largest first: which runtime, how big,
        how much of it is logs."""
        ranked = sorted(self.consumers, key=lambda c: c.mb, reverse=True)
        return [consumer for consumer in ranked if consumer.mb >= 0.5][:limit]

    def store_is_the_problem(self) -> bool:
        """Could this tree plausibly be why the disk is full?

        When it could not (it is a rounding error next to what the disk has used),
        saying so is the single most useful thing GC can tell a person who came
        here to free space — and it costs one line.
        """
        if not self.disk.known:
            return True             # unknowable: never send them looking elsewhere
        return self.store_mb() >= STORE_BLAME_SHARE * self.disk.used_mb

    def space_headline(self) -> str:
        """One line, for the operator standing in front of a full C: drive."""
        store = self.store_mb()
        if not self.disk.known:
            return f"這棵樹目前佔用 {store:,.0f} MB。"
        if not self.store_is_the_problem():
            return (f"這棵樹只佔 {store:,.0f} MB,{self.disk.label}的空間不是被它吃掉的"
                    f"(可用 {self.disk.free_mb:,.0f} MB)。")
        return (f"這棵樹目前佔用 {store:,.0f} MB;"
                f"{self.disk.label}可用 {self.disk.free_mb:,.0f} MB"
                f"{'(快滿了)' if self.disk.nearly_full() else ''}。")

    def space_lines(self) -> list[str]:
        """WHERE THE SPACE WENT — printed on every run, including the ones that
        reclaim nothing.

        「沒有可回收的項目」 is a true answer to a question the operator did not ask.
        They asked where their C: drive went. This is that answer: how full the disk
        is, how much of it is this tree, what the biggest pieces are, and how much
        of it is just logs nobody ever rotated.
        """
        lines = ["", "磁碟空間:", f"  {self.disk.line()}"]
        store = self.store_mb()
        note = "(至少;有檔案量不到大小)" if self.measurement_is_partial() else ""
        share = ""
        if self.disk.known and self.disk.used_mb >= 1:
            percent = 100.0 * store / self.disk.used_mb
            if percent >= 1:
                share = f",佔{self.disk.label}已用空間的 {percent:.0f}%"
        lines.append(f"  這棵樹(整個交付資料夾)目前佔用:{store:,.0f} MB{note}{share}")
        for consumer in self.biggest(5):
            lines.append(f"    - {consumer.line()}")
        if not self.applied:
            reclaim_logs, reclaim_cache = self.logs_mb(), self.cache_mb()
            if reclaim_logs >= 1 or reclaim_cache >= 1:
                lines.append(f"  其中舊記錄檔 {reclaim_logs:,.0f} MB、"
                             f"快取 {reclaim_cache:,.0f} MB 這次可以回收"
                             f"(記錄檔一直沒有人輪替,每啟動一次就多一份)")
        if self.applied and self.disk_after is not None and self.disk_after.known:
            lines.append(f"  {self.disk.label}可用空間:{self.disk.free_mb:,.0f} MB "
                         f"-> {self.disk_after.free_mb:,.0f} MB")
        if not self.store_is_the_problem():
            lines.append(f"  這棵樹只佔 {store:,.0f} MB,{self.disk.label}的空間不是被它"
                         "吃掉的:請往別處找(使用者的下載/桌面、Windows 更新暫存、"
                         "其他程式的資料夾)。")
        elif self.disk.nearly_full():
            lines.append(f"  {self.disk.label}快滿了(可用 {self.disk.free_mb:,.0f} MB)。")
        lines += self.unmeasured_lines()
        return lines

    def unmeasured_lines(self) -> list[str]:
        """Files we could not size. Silence here is what let one locked file turn a
        450 MB runtime into 「0 MB」 and send the operator away empty-handed."""
        if not self.measurement_is_partial():
            return []
        lines = [f"  有 {self.unmeasured_count} 個檔案量不到大小(通常是 App 還開著,"
                 "或防毒/檔案總管正在讀),所以上面的數字是「至少」,不是「全部」:"]
        lines += [f"    - {path}:{why}" for path, why in self.unmeasured[:5]]
        if self.unmeasured_count > 5:
            lines.append(f"    …還有 {self.unmeasured_count - 5} 個")
        return lines

    def exit_code(self) -> int:
        """What the caller (tools\\gc.bat) is told. See the contract table.

        Four different failures used to exit 2, so the bat could only ever print one
        story — 「沒有刪掉任何東西」 — and it was false for every partial run. And
        two opposite OUTCOMES used to exit 0 — "deleted every tree" and "deleted
        nothing at all" — so the bat printed 「回收完成。上面列出的項目都已經刪掉
        了。」 over a run that listed nothing and freed 0 bytes.
        """
        if self.is_empty():
            # Nothing to reclaim, whether or not --apply ran. On the DRY run this is
            # what stops the bat asking 「以上列出的項目要真的刪除嗎?」 about a blank
            # list; on --apply it is what stops it answering 「都已經刪掉了」.
            return EXIT_EMPTY_PLAN
        if not self.applied:
            return EXIT_OK                  # a plan with something in it: proceed
        if self.survivors:
            return EXIT_PARTIAL if self.deleted else EXIT_NOTHING_DELETED
        return EXIT_OK                      # every tree in the plan actually went

    # ── operator-facing text (plain, cp950-encodable: no emoji, no box-drawing) ──

    def headline(self) -> str:
        """ONE line, true, safe to show verbatim (a GUI status bar, a console tail).

        Every number in it after --apply is MEASURED from the trees that actually
        went away. reclaimable_mb() may never appear here: it is a forecast, and
        printing a forecast in the past tense is how operators came to believe they
        had reclaimed space that rmtree never managed to take.
        """
        if not self.applied:                                   # a plan, not an outcome
            if not self.is_empty():
                return (f"試算:可回收 {self.item_count()} 項、"
                        f"約 {self.reclaimable_mb():.0f} MB(還沒有刪除任何東西)。")
            if self.self_hosted:
                return (f"沒有其他可回收的項目;還有一份沒人在用的 runtime "
                        f"{self.self_hosted},但 GC 正在用它執行,這次回收不掉。")
            # An empty plan is NOT the end of the conversation. The person reading
            # this has a full disk; tell them where the space actually is.
            return f"沒有可回收的項目。{self.space_headline()}"
        if self.deleted and self.survivors:
            return (f"部分回收:刪掉 {len(self.deleted)} 項,"
                    f"實際回收 {self.reclaimed_mb():.0f} MB;"
                    f"還有 {len(self.survivors)} 項刪不掉,那些空間沒有回收。")
        if self.deleted:
            return (f"回收完成:刪掉 {len(self.deleted)} 項,"
                    f"實際回收 {self.reclaimed_mb():.0f} MB。")
        if self.survivors:
            return (f"一項都沒有刪掉:{len(self.survivors)} 項全部刪不掉,"
                    "磁碟空間完全沒有回收。")
        if self.self_hosted:
            return ("沒有刪掉任何東西:除了 GC 自己正在執行的那份 runtime 之外,"
                    "沒有可回收的項目。")
        return (f"沒有可回收的項目:這次沒有刪掉任何東西,磁碟空間沒有變化。"
                f"{self.space_headline()}")

    def scope_lines(self) -> list[str]:
        """Which apps this run even looked at. On a two-app store, a reclaim that
        silently ignored the other app is indistinguishable from one that found
        nothing there."""
        if not self.apps_all:
            return ["這個 store 裡沒有任何 app。"]
        lines = [f"掃描的 app:{'、'.join(self.apps_all)}"]
        skipped = [a for a in self.apps_all if a not in self.apps_considered]
        if skipped:
            lines.append(f"本次只回收(--app):{'、'.join(self.apps_considered) or '(無)'}")
            lines.append(f"不動的 app:{'、'.join(skipped)}"
                         f"(它們的版本一個都不會刪;它們用到的共用 runtime 也會保留)")
        else:
            lines.append(f"本次回收範圍:全部 {len(self.apps_all)} 個 app"
                         f"(要只回收其中一個:--app <app_id>)")
        return lines

    def keep_lines(self) -> list[str]:
        """The keep-set, WITH the reason. 「為什麼這個版本沒被回收?」 is the first
        thing an operator asks when GC frees less than they hoped, and the answer
        was computed on every run and shown on none of them."""
        lines: list[str] = ["保留的版本(不會刪除):"]
        for app_id in sorted(self.keep_versions):
            versions = self.keep_versions[app_id]
            if not versions:
                lines.append(f"  · {app_id}:(沒有任何版本被引用)")
                continue
            reasons = self.keep_reasons.get(app_id, {})
            for version in sorted(versions):
                why = "、".join(reasons.get(version) or ["(未知原因)"])
                lines.append(f"  · {app_id}/{version}:{why}")
        for app_id, version in self.skipped_versions:
            lines.append(f"  · {app_id}/{version}:不在 --app 範圍內,這次不動它")
        if self.unreadable:
            lines.append("為了安全,這次「不回收共用 runtime / shell」:"
                         "有版本樹讀不到 manifest,無法確定它們在用哪一份共用元件。")
            lines += [f"  · {label}:{why}" for label, why in self.unreadable]
        return lines

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
        size = (self.measure(self.self_hosted_path).text()
                if self.self_hosted_path else "0 MB")
        head = ("沒有其他可回收的項目,但是有一份沒人在用的 runtime 這次回收不掉:"
                if self.is_empty() else "另外有一份沒人在用的 runtime 這次回收不掉:")
        lines = [
            f"\n[注意] {head}",
            f"  runtime {self.self_hosted}({size})沒有任何版本引用它,"
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
        lines = self.scope_lines() + self.keep_lines()
        lines += [f"保留 runtime:{sorted(self.keep_fingerprints)}",
                  f"保留 shell:{sorted(self.keep_shells)}"]
        lines += [f"可刪版本:{a}/{v}({self.measure(p).text()})" for a, v, p in self.delete_versions]
        lines += [f"可刪 runtime:{fp}({self.measure(p).text()})" for fp, p in self.delete_runtimes]
        lines += [f"可刪 shell:{fp}({self.measure(p).text()})" for fp, p in self.delete_shells]
        lines += [f"可刪建置殘留:{w}({self.measure(p).text()})" for w, p in self.delete_staging]
        # Reported SEPARATELY from versions/runtimes, because they are a different
        # answer to a different question: this is the space a machine loses simply
        # by being used, and nobody was ever shown it.
        lines += [f"可刪舊記錄檔:{group.line()}" for group in self.delete_logs]
        lines += [f"可刪快取:{group.line()}(刪掉會自動重建,只是下次啟動稍慢)"
                  for group in self.delete_caches]
        if not self.is_empty():
            lines.append(f"可回收合計:{self.reclaimable_mb():.0f} MB")
        elif not self.self_hosted:
            # ONLY here is "nothing to reclaim" true. With self_hosted set, the
            # empty delete lists are not the absence of an orphan — they are the
            # orphan we are standing in.
            lines.append("沒有可回收的項目。")
        lines += self.space_lines()          # …and WHERE THE SPACE WENT, regardless
        if self.self_hosted:
            lines.append(self.self_hosted_note())
        return "\n".join(lines)

    def report(self) -> str:
        """What --apply ACTUALLY did. Past tense, measured from the trees that
        really went away — never the plan's 「可回收」 figure, which is a forecast
        and was printed verbatim after the fact even when every rmtree had failed.

        The three outcomes must be told apart in words as well as in the exit code:
        everything went / some went / nothing went. They were one sentence and one
        exit code, so a run that reclaimed 400 MB of 600 MB was reported to the
        operator as 「沒有刪掉任何東西」.
        """
        lines = self.scope_lines() + self.keep_lines()
        lines += [f"已刪除 {label}({mb:.0f} MB)" for label, mb in self.deleted]
        if self.deleted and not self.survivors:
            lines.append(f"實際回收合計:{self.reclaimed_mb():.0f} MB(計畫中的項目全部刪除完成)")
        elif self.deleted:
            lines.append(f"實際回收合計:{self.reclaimed_mb():.0f} MB")
            lines.append(f"部分回收:成功刪除 {len(self.deleted)} 項,"
                         f"還有 {len(self.survivors)} 項刪不掉(見下)。")
        elif not self.survivors and not self.self_hosted:
            # The empty plan, applied. There is no 「實際回收合計」 line here and
            # there must never be one: 0 MB came back. gc.bat used to be handed
            # EXIT_OK for this and answered 「上面列出的項目都已經刪掉了。」 about a
            # list with nothing in it.
            lines.append("沒有可回收的項目,沒有刪除任何東西。")
            lines.append("磁碟空間沒有變化(這不是錯誤:store 裡本來就沒有可回收的東西)。")
        elif self.survivors:
            lines.append(f"一項都沒有刪掉:{len(self.survivors)} 項全部刪不掉,"
                         f"磁碟空間完全沒有回收。")
        if self.survivors:
            lines.append("下列項目刪不掉,空間「沒有」回收:")
            lines += [f"  · {survivor.line()}" for survivor in self.survivors]
            lines.append("  最常見的原因:App 還開著,或檔案總管/防毒正在讀那個資料夾。")
            lines.append("  請把 App 完全關掉(所有視窗),再重跑一次。")
            if self.deleted:
                lines.append("  已經刪掉的那些不會再刪一次,重跑只會處理上面這幾項。")
        # Even after a reclaim that freed nothing, the question that brought them
        # here stands: WHERE DID THE DISK GO? (Past tense here — space_lines()
        # prints no forecast once applied is set.)
        lines += self.space_lines()
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


def _collect_logs(plan: GcPlan, app_id: str, paths: paths_mod.AppPaths) -> None:
    """The logs nobody ever rotated.

    Every launch writes launcher-*.log + streamlit-*.log + bootstrap-*.log into
    apps/<app>/data/logs, and NOTHING has ever deleted one. On a factory machine
    that has been up for months this is frequently the single biggest thing in the
    tree — and GC could not see it at all. The newest few of each family stay
    (those are the ones anyone actually reads, and a live session's log is always
    among them); the rest is offered for reclamation.
    """
    log_dir = paths.data_dir / "logs"
    doomed = stale_logs(log_dir)
    if not doomed:
        return
    group = GcGroup(kind="logs", app_id=app_id, root=log_dir, paths=doomed,
                    label=f"{app_id} 的舊記錄檔"
                          f"(每一種只留最近 {LOG_KEEP_RECENT} 份)")
    total = Measured()
    for path in doomed:
        total.add(plan.measure(path))
    group.mb, group.partial = total.mb, total.partial
    plan.delete_logs.append(group)


def _collect_cache(plan: GcPlan, app_id: str, paths: paths_mod.AppPaths) -> None:
    """data/cache — where PYTHONPYCACHEPREFIX puts every .pyc the app ever compiled.

    Pure derived data: deleting it costs one slightly slower start and nothing else.
    It is never in the keep-set, and it was never in the plan either.
    """
    cache = paths.data_dir / "cache"
    try:
        children = sorted(cache.iterdir())
    except OSError:
        return
    if not children:
        return
    group = GcGroup(kind="cache", app_id=app_id, root=cache, paths=children,
                    label=f"{app_id} 的快取(pycache)")
    total = Measured()
    for child in children:
        total.add(plan.measure(child))
    group.mb, group.partial = total.mb, total.partial
    plan.delete_caches.append(group)


def _measure_store(plan: GcPlan, root: Path, *, forecast: bool = True) -> None:
    """The whole tree, bucketed — so the operator can SEE where the disk went.

    Reported on every run, including the ones with an empty plan: 「沒有可回收的
    項目」 answers a question they did not ask. Every measurement here is cached and
    reused by the delete loop, so this costs one walk, not two.

    forecast=False is the re-measure after --apply: the trees are gone, so nothing
    here may carry a 「可以回收」 figure. A forecast printed in the past tense is the
    oldest lie in this module.
    """
    doomed_versions = {(app, version) for app, version, _p in plan.delete_versions}
    doomed_runtimes = {fp for fp, _p in plan.delete_runtimes}
    doomed_shells = {fp for fp, _p in plan.delete_shells}
    doomed_staging = {str(path) for _w, path in plan.delete_staging}
    logs_by_app = {group.app_id: group for group in plan.delete_logs}
    cache_by_app = {group.app_id: group for group in plan.delete_caches}

    def add(label: str, kind: str, measured: Measured, *, reclaimable: float = 0.0,
            path: Path | None = None) -> None:
        if measured.mb <= 0 and not measured.partial:
            return
        plan.consumers.append(Consumer(label=label, kind=kind, mb=measured.mb,
                                       reclaimable_mb=reclaimable if forecast else 0.0,
                                       partial=measured.partial,
                                       path=str(path) if path else None))

    for app_id in plan.apps_all:
        paths = paths_mod.AppPaths(root, app_id)
        versions, reclaim = Measured(), 0.0
        for child in _children(paths.versions_dir):
            measured = plan.measure(child)
            versions.add(measured)
            if (app_id, child.name) in doomed_versions or str(child) in doomed_staging:
                reclaim += measured.mb
        add(f"{app_id}:版本檔案", "versions", versions, reclaimable=reclaim,
            path=paths.versions_dir)

        staging, reclaim = Measured(), 0.0
        for child in _children(paths.staging_dir):
            measured = plan.measure(child)
            staging.add(measured)
            if str(child) in doomed_staging:
                reclaim += measured.mb
        add(f"{app_id}:建置/下載殘留", "staging", staging, reclaimable=reclaim,
            path=paths.staging_dir)

        logs = plan.measure(paths.data_dir / "logs")
        group = logs_by_app.get(app_id)
        add(f"{app_id}:記錄檔(logs)", "logs", logs,
            reclaimable=group.mb if group else 0.0, path=paths.data_dir / "logs")

        cache = plan.measure(paths.data_dir / "cache")
        group = cache_by_app.get(app_id)
        add(f"{app_id}:快取(cache)", "cache", cache,
            reclaimable=group.mb if group else 0.0, path=paths.data_dir / "cache")

        other = Measured()
        for child in _children(paths.data_dir):
            if child.name in ("logs", "cache"):
                continue                        # counted above, on their own
            other.add(plan.measure(child))
        for child in _children(paths.app_dir):
            if child.name in ("versions", "staging", "data"):
                continue
            other.add(plan.measure(child))
        add(f"{app_id}:其他資料(home/tmp/leases/state)", "data", other,
            path=paths.data_dir)

    runtimes = RuntimeStore(root / "deps").runtimes
    for child in _children(runtimes):
        measured = plan.measure(child)
        reclaim = measured.mb if (child.name in doomed_runtimes
                                  or str(child) in doomed_staging) else 0.0
        add(f"runtime {child.name}", "runtime", measured, reclaimable=reclaim,
            path=child)

    shells = ShellStore(root / "deps").shells
    for child in _children(shells):
        measured = plan.measure(child)
        reclaim = measured.mb if (child.name in doomed_shells
                                  or str(child) in doomed_staging) else 0.0
        add(f"shell {child.name}", "shell", measured, reclaimable=reclaim, path=child)

    for child in _children(root / "deps"):
        if child.name in ("runtimes", "shells"):
            continue
        add(f"deps\\{child.name}", "other", plan.measure(child), path=child)
    for child in _children(root):
        if child.name in ("apps", "deps"):
            continue
        add(f"{child.name}", "other", plan.measure(child), path=child)


def _children(path: Path) -> list:
    try:
        return sorted(Path(path).iterdir())
    except OSError:
        return []


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


def _keep_set(plan: GcPlan, app_id: str, paths: paths_mod.AppPaths,
              state: state_mod.AppState) -> set:
    """Every version this app still needs, AND why — the reason is half the point."""
    keep: set = set()
    reasons: dict = {}

    def pin(version: str | None, label: str) -> None:
        if not version:
            return
        keep.add(version)
        why = reasons.setdefault(version, [])
        if label not in why:
            why.append(label)

    for attr, label in KEEP_LABELS:
        pin(getattr(state, attr), label)
    for lease in leases.valid_leases(paths.data_dir / "leases"):
        pin(lease.get("version"), KEEP_LEASE)
        if lease.get("runtime_fingerprint"):
            plan.keep_fingerprints.add(lease["runtime_fingerprint"])

    plan.keep_versions[app_id] = keep
    plan.keep_reasons[app_id] = reasons
    return keep


def collect_plan(root: Path, *, apps: list | None = None) -> GcPlan:
    root = Path(root)
    all_apps = paths_mod.list_app_ids(root)
    if apps:
        unknown = [a for a in apps if a not in all_apps]
        if unknown:
            raise GcError(f"找不到 app:{'、'.join(unknown)}。"
                          f"這個 store 裡有:{'、'.join(all_apps) or '(沒有任何 app)'}")
        selected = [a for a in all_apps if a in set(apps)]
    else:
        selected = list(all_apps)

    plan = GcPlan(root=root, apps_all=list(all_apps), apps_considered=selected)
    # This interpreter's own runtime is never deletable: rmtree'ing the tree we
    # execute from would leave a half-dead store (open-image deletes fail).
    own_prefix = Path(sys.prefix).resolve()

    for app_id in all_apps:
        paths = paths_mod.AppPaths(root, app_id)
        state = state_mod.StateStore(paths.state_dir).load()  # broken state → loud abort
        # The keep-set is computed for EVERY app, even the ones --app excluded: a
        # shared runtime belongs to the machine, and one app's --app-scoped reclaim
        # must never delete the interpreter another app is about to start under.
        keep = _keep_set(plan, app_id, paths, state)
        scoped_in = app_id in selected

        on_disk = []
        if paths.versions_dir.is_dir():
            for child in sorted(paths.versions_dir.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue  # .staging-* — collected below, reported as such
                on_disk.append((child.name, child))

        doomed = set()
        for version, child in on_disk:
            if version in keep:
                continue
            if scoped_in:
                plan.delete_versions.append((app_id, version, child))
                doomed.add(version)
            else:
                plan.skipped_versions.append((app_id, version))

        # Every version tree that is STILL on disk when this run finishes pins its
        # runtime and its shell. In a full run that is exactly the keep-set. In an
        # --app-scoped run it also covers the other apps' unreferenced leftovers,
        # which we are deliberately NOT deleting — so deleting the shared runtime
        # underneath them would leave intact-looking versions that cannot start,
        # and that --rollback-to would happily offer.
        for version, _child in on_disk:
            if version in doomed:
                continue
            try:
                runtime_fp, shell_fp = _fingerprints_of(paths, version)
            except (GcError, paths_mod.LayoutError, IdentifierError,
                    OSError, ValueError) as exc:
                if version in keep:
                    raise  # a live slot we cannot read: refuse, never guess
                plan.unreadable.append((f"{app_id}/{version}", str(exc)))
                continue
            plan.keep_fingerprints.add(runtime_fp)
            if shell_fp:
                plan.keep_shells.add(shell_fp)

        # store_builder stages a version under versions/.staging-*; the updater
        # stages under apps/<app>/staging/<hex> (no dot prefix: the whole dir is
        # scratch space, so every child of it is reclaimable). Scratch is scratch
        # in any app, but --app scoping still only touches what it was pointed at.
        if scoped_in:
            _collect_staging(plan, f"{app_id}/versions", paths.versions_dir)
            _collect_staging(plan, f"{app_id}/staging", paths.staging_dir, prefixed=False)
            # The two things that ACTUALLY fill a long-running machine, and the two
            # things GC never looked at: unrotated logs and the bytecode cache.
            _collect_logs(plan, app_id, paths)
            _collect_cache(plan, app_id, paths)

    runtimes = RuntimeStore(root / "deps").runtimes
    shells = ShellStore(root / "deps").shells
    # A version tree we could not read pins an unknown runtime. Deleting shared
    # trees on a guess is the one mistake this module exists to prevent, so we
    # reclaim nothing shared this run and say exactly why (keep_lines()).
    if runtimes.is_dir() and not plan.unreadable:
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

    if shells.is_dir() and not plan.unreadable:
        for child in sorted(shells.iterdir()):
            if child.is_dir() and not child.name.startswith(".") \
                    and child.name not in plan.keep_shells:
                plan.delete_shells.append((child.name, child))

    _collect_staging(plan, "deps/runtimes", runtimes)
    _collect_staging(plan, "deps/shells", shells)

    # WHERE THE SPACE WENT. Measured last, so it can mark each consumer with how
    # much of it this run could actually take back — and reported even when the
    # answer is 「none of it」, because that is still the answer to their question.
    plan.disk = disk_space(root)
    _measure_store(plan, root)
    return plan


def _delete_tree(label: str, path: Path) -> list:
    """Delete a tree; return the GcSurvivor that stopped us, if any.

    This used to be shutil.rmtree(ignore_errors=True), which turns "the App is
    still running / an antivirus has the folder open / Explorer is sitting in it"
    into silence — and GC then printed 「可回收 480 MB」 with all 480 MB still on
    the disk. A GC that cannot delete something must SAY so; the operator can
    close the app and run it again, but only if they are told.
    """
    path = Path(path)
    if path.is_file():
        # A log file, not a tree. There is no sentinel to strip, and rmtree would
        # raise NotADirectoryError — which we would then report to the operator as
        # though their App were holding the file open.
        try:
            os.remove(path)
        except OSError as exc:
            return [GcSurvivor(label, str(path), _why(exc), _in_use(exc))]
        return []
    try:
        integrity.remove_complete(path)  # first: make it invisible (fail closed)
    except OSError as exc:
        return [GcSurvivor(label, str(path), f"連 .complete 都刪不掉({_why(exc)})",
                           _in_use(exc))]
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return [GcSurvivor(label, str(path), _why(exc), _in_use(exc))]
    if path.exists():
        # rmtree said nothing and the folder is still there: on Windows that is a
        # delete pending behind somebody's open handle. Treat it as in-use — that
        # is both the usual cause and the only advice that can help.
        return [GcSurvivor(label, str(path), "資料夾還在(可能有程式正開著它)", True)]
    return []


def run_gc(root: Path, *, apps: list | None = None, apply: bool = False,
           log=print) -> GcPlan:
    root = Path(root)
    # The updater takes this very lock around staging (updater._STAGE_LOCK_TIMEOUT),
    # so a runtime being downloaded right now cannot be swept away half-written.
    with store_gc_lock(root / "deps"):
        plan = collect_plan(root, apps=apps)   # scan INSIDE the lock (spec §11)
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
            survivors = _delete_tree(label, path)
            if survivors:
                plan.survivors.extend(survivors)
            else:
                # Only a tree that actually went away counts towards reclaimed_mb().
                plan.deleted.append((label, size))
        for group in plan.groups():
            _delete_group(plan, group, log)
        # The disk, re-measured: this is the one number the operator came for, and
        # it is the only one that cannot be argued with. The TREE is re-measured
        # too — the breakdown we scanned describes a tree that no longer exists.
        plan.disk_after = disk_space(root)
        plan.refresh_usage(root)
        # Past tense, from what actually happened — not the forecast we printed
        # before touching anything.
        _emit(log, plan.report())
    return plan


def _delete_group(plan: GcPlan, group: GcGroup, log) -> None:
    """Delete a bulk group (old logs, cache) file by file, report it as one thing.

    Per-FILE deletion so a single locked log costs us that log and not the other
    399; per-GROUP reporting so the console does not scroll 400 lines of
    「刪除 舊記錄檔 …」 past an operator who wanted one number.
    """
    _emit(log, f"刪除 {group.label}({group.count} 個,{group.mb:,.0f} MB)…")
    freed, failures = 0.0, []
    for path in group.paths:
        size = plan._mb(path)            # from the collect-time walk: it existed then
        survivors = _delete_tree(group.label, path)
        if survivors:
            failures.extend(survivors)
        else:
            freed += size
    gone = group.count - len(failures)
    if gone:
        plan.deleted.append((f"{group.label}(刪掉 {gone} 個)", freed))
    if failures:
        # One survivor for the group, not one per file: 「12 個刪不掉」 is the fact,
        # and the reason is the same for all twelve.
        plan.survivors.append(GcSurvivor(
            group.label, str(group.root),
            f"{len(failures)} 個檔案刪不掉({failures[0].reason})",
            any(survivor.in_use for survivor in failures)))


def _store_root() -> Path:
    """<ROOT> of the deployed tree: gc.py lives in <ROOT>\\bootstrap\\."""
    return Path(__file__).resolve().parents[1]


def main(argv: list | None = None) -> int:
    """The CLI. Every outcome gets its own exit code (module docstring) — one code
    for four different disasters is how tools\\gc.bat came to print
    「沒有刪掉任何東西」 over a run that had just reclaimed 400 MB."""
    import argparse
    parser = argparse.ArgumentParser(
        description="回收未被任何槽引用的版本與 runtime(預設只列出,不刪除)")
    parser.add_argument("--apply", action="store_true", help="真的刪除(預設只列出)")
    parser.add_argument("--app", action="append", metavar="APP_ID",
                        help="只回收這個 app 的版本(可重複指定);"
                             "省略就是全部。其他 app 的版本一律保留,"
                             "它們用到的共用 runtime 也一律保留")
    args = parser.parse_args(argv)

    try:
        plan = run_gc(_store_root(), apps=args.app, apply=args.apply)
    except LockTimeout:
        print("\n[gc][ERROR] 目前有更新正在下載或安裝(store 鎖被佔用),"
              "所以這次「連掃描都沒有做」,沒有刪除任何東西。\n"
              "  這不是錯誤,也不用做任何事:請等更新完成後再重跑一次。", file=sys.stderr)
        return EXIT_STORE_LOCKED
    except (GcError, state_mod.StateError, paths_mod.LayoutError,
            IdentifierError, UnicodeEncodeError) as exc:
        # A traceback tells a factory IT nothing. Say what is wrong and that
        # NOTHING was deleted — GC aborts whole rather than guess.
        # UnicodeEncodeError is in this list because the console, not the store,
        # is the thing that broke: cp950 cannot print every character. _emit()
        # should have absorbed it, so reaching here means a message we do not
        # control leaked out — say so instead of dumping a traceback.
        print(f"\n[gc][ERROR] {exc}\n"
              "  為了安全,這次沒有刪除任何東西(一項都沒有刪)。\n"
              "  請先修好上面提到的問題,再重跑一次。", file=sys.stderr)
        return EXIT_ABORTED
    except OSError as exc:
        print(f"\n[gc][ERROR] 磁碟操作失敗:{_why(exc)}\n"
              "  掃描階段就失敗了,沒有刪除任何東西。", file=sys.stderr)
        return EXIT_ABORTED

    code = plan.exit_code()
    # plan.report() has already listed every survivor and why. The headline here is
    # what a wrapper (tools\gc.bat) echoes, so it must not contradict it — and every
    # number in it is MEASURED (reclaimed_mb()), never the plan's forecast.
    if code == EXIT_PARTIAL:
        print(f"\n[gc][注意] 部分回收:已經刪掉 {len(plan.deleted)} 項"
              f"(實際回收 {plan.reclaimed_mb():.0f} MB),"
              f"另外 {len(plan.survivors)} 項刪不掉,那些空間沒有回收。\n"
              "  上面列出了刪不掉的是哪幾個、為什麼。最常見的是「檔案使用中」:"
              "請把 App 完全關掉(所有視窗)再重跑一次。", file=sys.stderr)
    elif code == EXIT_NOTHING_DELETED:
        print(f"\n[gc][ERROR] 一項都沒有刪掉:{len(plan.survivors)} 項全部刪不掉,"
              "磁碟空間完全沒有回收。\n"
              "  最常見的原因:App 還開著,或檔案總管/防毒正在讀那個資料夾。\n"
              "  請把 App 完全關掉(所有視窗),再重跑一次。", file=sys.stderr)
    elif code == EXIT_EMPTY_PLAN:
        # Not stderr: nothing went wrong. But it must not be silent either — an
        # operator who ran GC to get disk space back is owed a plain statement that
        # they are not going to get any, and why that is fine.
        if plan.self_hosted:
            because = "  上面說明了那份 runtime 為什麼這次回收不掉,以及要怎麼把它收掉。"
        elif plan.applied:
            because = ("  這不是錯誤:store 裡沒有任何沒被引用的版本或 runtime,"
                       "所以這次沒有刪掉任何東西,磁碟空間也沒有變化。")
        else:
            because = "  這不是錯誤:store 裡沒有任何沒被引用的版本或 runtime。"
        # An empty plan is not the end of the conversation. They came here with a
        # full disk; the summary above has already printed where the space went.
        if not plan.store_is_the_problem():
            because += ("\n  上面的「磁碟空間」那一段是重點:這棵樹不是把"
                        f"{plan.disk.label}塞滿的元凶,請往別的地方找。")
        elif plan.disk.nearly_full():
            because += ("\n  但是磁碟真的快滿了(見上面的「磁碟空間」):"
                        "上面列出了這棵樹裡最大的幾項,請照著它們去處理。")
        print(f"\n[gc] {plan.headline()}\n{because}")
    elif code == EXIT_OK and plan.applied:
        print(f"\n[gc] {plan.headline()}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""File locks with stale-owner detection.

A lock is a file whose body records who owns it. "Who" is PID **plus process
creation time**: Windows reuses PIDs, so a bare PID check would let a newborn
unrelated process keep a dead updater's lock alive forever (spec §10.1).
Creation time comes from GetProcessTimes via ctypes — stdlib only.

Two claim mechanisms, in order of preference:

1. **hardlink** (`os.link` of a fully-written tmp file). Race-free: the lock
   appears with its complete body or not at all, so a waiter can never read a
   half-written owner record. This is what NTFS gets.
2. **O_CREAT|O_EXCL** fallback, for FAT/exFAT — i.e. most USB sticks, and spec
   §9.3 promises "FAT/exFAT USB 皆可". `os.link` there raises OSError and used to
   hand the operator a raw traceback. O_EXCL is still exclusive, but it exposes
   a zero-length lock file for the instant between create and write, so the
   READER must treat an empty lock as "claim in progress → wait", never as
   "garbage → steal" (see _break_if_stale).
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import time
import uuid
from pathlib import Path


class LockTimeout(Exception):
    pass


# ── process identity ─────────────────────────────────────────────────────────

def process_start_time(pid: int) -> int | None:
    """Creation FILETIME of `pid` as an int; None if the process is gone.

    ERROR_ACCESS_DENIED means "alive but not ours" — we return a sentinel so
    callers treat the lock as NOT stale (never steal what might be running).
    """
    if os.name != "nt":  # pragma: no cover - test/dev convenience
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return -1
        return -1

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        ERROR_ACCESS_DENIED = 5
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED or kernel32.GetLastError() == ERROR_ACCESS_DENIED:
            return -1  # alive, unknown start time — not stale
        return None
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        )
        if not ok:
            return -1
        return (created.high << 32) | created.low
    finally:
        kernel32.CloseHandle(handle)


def my_identity() -> dict:
    return {"pid": os.getpid(), "process_start_time": process_start_time(os.getpid())}


def owner_is_stale(meta: dict) -> bool:
    """True only when we are POSITIVE the recorded owner is gone."""
    pid = meta.get("pid")
    if not isinstance(pid, int):
        return True  # unreadable metadata: fail toward recovery, the file
        # was ours to manage and a garbage body means a torn write
    current = process_start_time(pid)
    if current is None:
        return True  # no such process
    if current == -1:
        return False  # alive (or unknowable) — never steal
    recorded = meta.get("process_start_time")
    return recorded is not None and recorded != -1 and current != recorded


# ── the lock ─────────────────────────────────────────────────────────────────

# errnos/winerrors that mean "this filesystem cannot do hard links" rather than
# "this particular link failed". FAT/exFAT on Windows surfaces as ERROR_ACCESS_DENIED
# (5) or ERROR_NOT_SUPPORTED (50); Python may also map them to EPERM/EACCES.
_NO_HARDLINK_ERRNOS = {
    errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EINVAL, errno.EXDEV, errno.EMLINK,
    getattr(errno, "EOPNOTSUPP", errno.ENOSYS), getattr(errno, "ENOTSUP", errno.ENOSYS),
}
_NO_HARDLINK_WINERRORS = {
    1,      # ERROR_INVALID_FUNCTION  — what exFAT usually gives
    5,      # ERROR_ACCESS_DENIED
    50,     # ERROR_NOT_SUPPORTED
    87,     # ERROR_INVALID_PARAMETER
    4390,   # ERROR_NOT_A_REPARSE_POINT (seen on some removable stacks)
}


def hardlinks_unsupported(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _NO_HARDLINK_WINERRORS
    return exc.errno in _NO_HARDLINK_ERRNOS


class FileLock:
    """Exclusive lock file. Reentrancy is NOT supported on purpose: each logical
    operation acquires once and holds until done."""

    # An unreadable (corrupt) body may be taken over after this long.
    _GARBAGE_GRACE = 5.0
    # An EMPTY body is the O_EXCL fallback's create-then-write window, so it is
    # normally microseconds old and belongs to a LIVE claimer. Only a process
    # killed inside that window can leave one behind permanently, so the grace
    # is long enough that we never race a live claimer, but finite so a power
    # cut cannot deadlock the store forever.
    _EMPTY_GRACE = 30.0

    def __init__(self, path: Path, *, what: str = "lock"):
        self.path = Path(path)
        self.what = what
        self._token: str | None = None
        self._use_hardlink = True

    def acquire(self, timeout: float = 30.0, poll: float = 0.2) -> "FileLock":
        deadline = time.monotonic() + timeout
        token = uuid.uuid4().hex
        body = json.dumps({**my_identity(), "operation_id": token,
                           "created_at": time.time(), "what": self.what})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".claim-{token}.tmp")
        try:
            while True:
                if self._claim(tmp, body):
                    self._token = token
                    return self
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{self.what} held by another process: {self.path}")
                time.sleep(poll)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _claim(self, tmp: Path, body: str) -> bool:
        """One attempt. True = we own the lock. False = held; caller retries."""
        if self._use_hardlink:
            try:
                if not tmp.exists():
                    tmp.write_text(body, encoding="utf-8")
                os.link(tmp, self.path)
            except FileExistsError:
                self._break_if_stale()
                return False
            except OSError as exc:
                if not hardlinks_unsupported(exc):
                    raise LockTimeout(
                        f"無法建立鎖檔:{self.path}({exc})") from exc
                # FAT/exFAT (a USB stick): no hard links. Degrade to O_EXCL for
                # the rest of this lock's life instead of dying on errno 1.
                self._use_hardlink = False
            else:
                return True
        return self._claim_exclusive(body)

    def _claim_exclusive(self, body: str) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._break_if_stale()
            return False
        except OSError as exc:
            raise LockTimeout(f"無法建立鎖檔:{self.path}({exc})") from exc
        # The file exists but is EMPTY until the next few lines land — that is
        # exactly the window _break_if_stale refuses to steal.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def _break_if_stale(self) -> None:
        try:
            raw = self.path.read_text("utf-8")
        except FileNotFoundError:
            return  # released while we looked — retry the claim
        except OSError:
            return  # momentarily unreadable (sharing violation) — retry

        if not raw.strip():
            # Zero-length: an O_EXCL claim in progress. The owner is alive and
            # about to write its identity; stealing here would hand the same
            # lock to two processes. Wait it out.
            if self._age() < self._EMPTY_GRACE:
                return
            meta = {}  # abandoned mid-claim (killed between create and write)
        else:
            try:
                meta = json.loads(raw)
            except ValueError:
                # A torn/corrupt body cannot happen with hardlink claims; treat
                # it as a crashed foreign writer only once it has sat a while.
                if self._age() < self._GARBAGE_GRACE:
                    return
                meta = {}

        if owner_is_stale(meta):
            try:
                os.remove(self.path)  # racing removers are fine: one wins,
            except OSError:           # the loop retries the claim either way
                pass

    def _age(self) -> float:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return 0.0  # gone or unreadable: treat as fresh, never steal

    def release(self) -> None:
        if self._token is None:
            return
        # A waiter momentarily holding the file open for its stale-check makes
        # os.remove throw a sharing violation. Giving up there would orphan a
        # lock whose recorded owner is alive — unbreakable until we die. Retry.
        for _ in range(100):
            try:
                meta = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                break  # already gone (or unreadable — the grace path handles it)
            if meta.get("operation_id") != self._token:
                break  # not ours anymore
            try:
                os.remove(self.path)
                break
            except PermissionError:
                time.sleep(0.02)
            except OSError:
                break
        self._token = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


@contextlib.contextmanager
def held(lock: FileLock, timeout: float = 30.0):
    """`with held(some_lock(...), timeout=120): ...`

    Use this instead of `with some_lock(...).acquire(timeout=120):` — that form
    looks right and deadlocks: `with` calls __enter__, which acquires a SECOND
    time, and the lock is not reentrant, so it waits out the full timeout on
    itself and raises LockTimeout.
    """
    lock.acquire(timeout=timeout)
    try:
        yield lock
    finally:
        lock.release()


def app_lock(state_dir: Path) -> FileLock:
    return FileLock(Path(state_dir) / "update.lock", what="app update lock")


def runtime_lock(runtimes_dir: Path, fingerprint: str) -> FileLock:
    locks = Path(runtimes_dir) / ".locks"
    return FileLock(locks / f"{fingerprint}.lock", what=f"runtime {fingerprint} lock")


def store_gc_lock(deps_dir: Path) -> FileLock:
    """Store-wide: held by GC while it scans+deletes, and by the updater while it
    stages. It guards runtimes AND shells AND version staging, so it lives at the
    store root — inside deps/runtimes/ it was both a lock on a thing it protects
    and an unexpected entry in every listing of the runtime store.
    """
    return FileLock(Path(deps_dir) / ".locks" / "gc.lock", what="store GC lock")

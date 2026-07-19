"""Windows-lock-aware filesystem moves (P2: shared Defender/lock fallback).

Windows returns ERROR_ACCESS_DENIED / SHARING_VIOLATION while *anything* still
holds a handle inside a tree — and Defender reliably does right after hundreds
of megabytes were written, as do dying child processes for a moment after
taskkill. Those locks are transient; failing an operation that actually
succeeded, or reporting a delete that silently did nothing, are both worse than
waiting. The builder learned this on real machines
(`streamlit_desktop/builder.py`); this module is the shared, reusable form for
the Native Agent and any new call site.

Stdlib-only (SPEC D2).
"""

from __future__ import annotations

import errno
import os
import shutil
import time
from pathlib import Path
from typing import Callable

OnWait = Callable[[str], None]

_LOCK_WINERRORS = {5, 32, 33}  # ACCESS_DENIED, SHARING_VIOLATION, LOCK_VIOLATION


def is_transient_lock(exc: OSError) -> bool:
    """Is this the failure an operator can fix by waiting / closing the app?"""
    winerror = getattr(exc, "winerror", None)
    return bool(
        isinstance(exc, PermissionError)
        or winerror in _LOCK_WINERRORS
        or exc.errno in (errno.EACCES, errno.EPERM, errno.EBUSY)
    )


class StillLocked(OSError):
    """The path stayed locked for the whole retry window."""

    def __init__(self, message: str, *, waited: float, last: OSError | None):
        super().__init__(message)
        self.waited = waited
        self.last = last


def robust_rename(src: Path | str, dst: Path | str, *,
                  attempts: int = 12, on_wait: OnWait | None = None) -> float:
    """`os.rename` with exponential backoff on transient locks.

    Returns the seconds spent waiting (0.0 when it went through first try).
    Raises :class:`StillLocked` when the window runs out, and re-raises
    non-lock errors (e.g. FileExistsError on Windows) immediately — those are
    the caller's semantics, not scanner noise.
    """
    src, dst = Path(src), Path(dst)
    delay, waited = 0.5, 0.0
    last: OSError | None = None
    for attempt in range(1, attempts + 1):
        try:
            os.rename(src, dst)
            return waited
        except OSError as exc:
            if not is_transient_lock(exc):
                raise
            last = exc
        if attempt == attempts:
            break
        if attempt == 2 and on_wait is not None:
            on_wait("檔案被系統暫時鎖住（防毒掃描或剛結束的子程序），等它放行…")
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 5.0)
    raise StillLocked(
        f"搬移被系統擋住（等了 {waited:.0f} 秒仍未放行）：{dst}\n"
        "  幾乎都是防毒軟體還在掃描剛寫好的檔案。內容其實已就緒，稍後重試即可；\n"
        "  或請 IT 把此資料夾加入防毒排除清單。",
        waited=waited, last=last,
    )


def robust_rmtree(path: Path | str, *, attempts: int = 8,
                  on_wait: OnWait | None = None) -> bool:
    """Delete a tree with backoff, then CHECK. True only when it is really gone.

    `shutil.rmtree(ignore_errors=True)` is not a delete, it is a wish — the
    caller must know when space was NOT reclaimed so it can defer, not lie.
    """
    path = Path(path)
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return True
        except OSError:
            pass  # locked or mid-scan: back off and retry
        if not path.exists():
            return True
        if attempt == attempts:
            return False
        if attempt == 2 and on_wait is not None:
            on_wait("目錄還被系統鎖住（防毒或未收尾的程序），等它放行…")
        time.sleep(delay)
        delay = min(delay * 2, 5.0)
    return not path.exists()

"""User notification: one OK-button message box, best effort.

A failed notification must never fail the update transaction (spec §9.2) —
state.pending is already written; the promote happens next start regardless.
"""

from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger("notify")

_MB_OK = 0x0
_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000


def notify(title: str, message: str) -> bool:
    log.info("notify: %s — %s", title, message.replace("\n", " / "))
    if os.name != "nt":  # pragma: no cover
        return False
    try:
        ctypes.windll.user32.MessageBoxW(
            None, message, title, _MB_OK | _MB_ICONINFORMATION | _MB_SETFOREGROUND | _MB_TOPMOST)
        return True
    except Exception as exc:  # noqa: BLE001 - by contract: never propagate
        log.error("MessageBox failed: %s", exc)
        return False

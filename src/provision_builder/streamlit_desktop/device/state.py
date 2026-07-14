"""The one authoritative record of which version is which.

`state.json` replaces PROD.txt/PREV.txt/NEXT.txt (spec §16.1): promote must
flip current+previous+pending+candidate in ONE atomic write, which three
separate files cannot do. Writes go through StateStore only — tmp file, fsync,
os.replace, read-back — so at every instant the file on disk is a complete,
valid state (old or new, never torn).

Transitions live here as pure functions so the whole PROD/PREV/NEXT/LKG state
machine is unit-testable without a filesystem.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

if __package__:
    from . import locks as locks_mod
    from .identifiers import IdentifierError, validate_identifier, validate_optional
else:  # loose files in <ROOT>/bootstrap/
    import locks as locks_mod
    from identifiers import IdentifierError, validate_identifier, validate_optional

SCHEMA_VERSION = 1
STATE_NAME = "state.json"


class StateError(Exception):
    pass


@dataclass
class AppState:
    app_id: str
    current: str
    previous: str | None = None
    pending: str | None = None
    # The revision travels with the version through pending → candidate →
    # failed_versions. Without it, a rolled-back version's failure is recorded
    # revision-less and the very next updater pass would happily re-stage the
    # SAME broken build (the first real E2E caught exactly that loop).
    pending_revision: str | None = None
    candidate: str | None = None
    candidate_revision: str | None = None
    last_known_good: str | None = None
    failed_versions: list = field(default_factory=list)  # [{"version","revision"}]
    generation: int = 1
    schema_version: int = SCHEMA_VERSION
    last_operation: dict = field(default_factory=dict)

    # -- queries ---------------------------------------------------------------

    def is_failed(self, version: str, revision: str | None = None) -> bool:
        """Spec §8.2: a failed version may only be retried under a new revision.
        An entry whose revision is unknown blocks every revision of that version
        — better a manual retry than an automatic crash loop."""
        for entry in self.failed_versions:
            if entry.get("version") == version:
                if revision is None or entry.get("revision") is None:
                    return True
                if entry.get("revision") == revision:
                    return True
        return False

    def rollback_target(self) -> str | None:
        """The best version to fall back to, from state alone.

        It must never be `current` (rolling back to the version we are rolling
        away from is a no-op that reports success), and never a version already
        in failed_versions. Both used to be possible: a version was committed as
        last_known_good the moment it printed one healthy marker, so a build that
        starts and then misbehaves became its own rollback target.

        Callers with a filesystem (bootstrap) should prefer
        bootstrap.resolve_rollback_target(), which additionally verifies the
        candidate on disk and can fall back to any other intact version dir.
        """
        for version in (self.last_known_good, self.previous):
            if version and version != self.current and not self.is_failed(version):
                return version
        return None

    # -- serialization ---------------------------------------------------------

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict, *, source: Path | None = None) -> "AppState":
        """`source` is the file this came from — it is the first thing the operator
        needs and the first thing these messages used to leave out."""
        where = f":{source}" if source else ""
        if not isinstance(data, dict):
            raise StateError(f"狀態檔內容不是一個 JSON 物件{where}")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise StateError(
                f"狀態檔的 schema 版本不支援:{data.get('schema_version')!r}"
                f"(這個版本的程式只認得 {SCHEMA_VERSION}){where}\n"
                "  這通常表示程式被降級了:請改用比較新的版本。")
        try:
            state = cls(
                app_id=validate_identifier(data.get("app_id"), "app_id"),
                current=validate_identifier(data.get("current"), "current"),
                previous=validate_optional(data.get("previous"), "previous"),
                pending=validate_optional(data.get("pending"), "pending"),
                pending_revision=_optional_str(data.get("pending_revision")),
                candidate=validate_optional(data.get("candidate"), "candidate"),
                candidate_revision=_optional_str(data.get("candidate_revision")),
                last_known_good=validate_optional(data.get("last_known_good"), "last_known_good"),
                failed_versions=list(data.get("failed_versions") or []),
                generation=int(data.get("generation", 0)),
                last_operation=dict(data.get("last_operation") or {}),
            )
        except IdentifierError as exc:
            raise StateError(f"狀態檔裡有不合法的名稱:{exc}{where}") from exc
        if state.generation < 1:
            raise StateError(
                f"狀態檔的 generation 必須 >= 1,讀到 {state.generation}{where}")
        return state


# ── pure transitions (spec §8) ───────────────────────────────────────────────

def _op(kind: str) -> dict:
    return {"id": uuid.uuid4().hex, "kind": kind, "status": "completed",
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _optional_str(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 200:
        raise StateError(f"revision must be a short string: {value!r}")
    return value


def promote_pending(state: AppState) -> AppState:
    """previous←current, current←pending, candidate=new current. LKG untouched."""
    if not state.pending:
        raise StateError("nothing to promote: pending is empty")
    return dataclasses.replace(
        state, previous=state.current, current=state.pending,
        pending=None, pending_revision=None,
        candidate=state.pending, candidate_revision=state.pending_revision,
        last_operation=_op("promote"),
    )


def clear_bad_pending(state: AppState, *, revision: str | None = None) -> AppState:
    """Pending failed verification: drop it, remember it, keep running current."""
    if not state.pending:
        return state
    failed = state.failed_versions + [
        {"version": state.pending, "revision": revision or state.pending_revision}]
    return dataclasses.replace(state, pending=None, pending_revision=None,
                               failed_versions=failed,
                               last_operation=_op("clear_bad_pending"))


def commit_candidate(state: AppState) -> AppState:
    """Health check passed: this version is now the last known good."""
    return dataclasses.replace(state, candidate=None, candidate_revision=None,
                               last_known_good=state.current,
                               last_operation=_op("commit_candidate"))


def fail_candidate(state: AppState, *, revision: str | None = None,
                   target: str | None = None) -> AppState:
    """Candidate failed its first health check: roll current back.

    `target` lets bootstrap supply a version it resolved against the filesystem
    (an intact version dir that state alone knows nothing about). Without it we
    fall back to the state-only answer.
    """
    if not state.candidate or state.candidate != state.current:
        raise StateError("no active candidate to fail")
    target = target or state.rollback_target()
    if not target:
        raise StateError("cannot roll back: no last_known_good or previous")
    if target == state.current:
        raise StateError(f"cannot roll back: {target} is the failing version")
    failed = state.failed_versions + [
        {"version": state.candidate, "revision": revision or state.candidate_revision}]
    last_known_good = state.last_known_good
    if last_known_good == state.candidate:
        last_known_good = None  # it was never good; do not offer it as a target
    return dataclasses.replace(state, current=target, candidate=None,
                               candidate_revision=None,
                               last_known_good=last_known_good,
                               failed_versions=failed, last_operation=_op("rollback"))


def rollback_to(state: AppState, target: str, *, revision: str | None = None) -> AppState:
    """Operator-driven move to `target`, marking the version we leave behind.

    Recording the version we roll AWAY from in failed_versions is not optional:
    without it the background updater sees the same release on the share, sees
    nothing in failed_versions, and re-stages the exact build the operator just
    rolled back from — the same day. (`--clear-failed` un-does this once the
    build is fixed.)
    """
    validate_identifier(target, "rollback target")
    leaving = state.current
    if target == leaving:
        raise StateError(f"{target} is already current")
    failed = state.failed_versions + [{"version": leaving, "revision": revision}]
    last_known_good = state.last_known_good
    if last_known_good == leaving:
        last_known_good = None  # demonstrably not good — the operator just fled it
    return dataclasses.replace(
        state, current=target, previous=leaving,
        # A pending copy of the version we are fleeing would be promoted right
        # back over us on the next start.
        pending=None if state.pending in (leaving, target) else state.pending,
        pending_revision=None if state.pending in (leaving, target) else state.pending_revision,
        candidate=None, candidate_revision=None,
        last_known_good=last_known_good, failed_versions=failed,
        last_operation=_op("manual_rollback"))


def set_pending(state: AppState, version: str, *, revision: str | None = None) -> AppState:
    validate_identifier(version, "pending")
    if version == state.current:
        raise StateError(f"{version} is already current")
    return dataclasses.replace(state, pending=version,
                               pending_revision=_optional_str(revision),
                               last_operation=_op("set_pending"))


# ── the store ────────────────────────────────────────────────────────────────

class StateStore:
    """All reads and writes of state.json. Writers must hold the app lock."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / STATE_NAME

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> AppState:
        """Read state.json, or say — in the operator's language, and about a file
        they can actually go and look at — why we could not.

        These messages end up on a factory machine's console, read out loud over
        the phone by the person standing in front of it. 「corrupt state.json」 with
        no path in it told them nothing: not which app, not which file, not what to
        do. Every error here now names the FILE and the NEXT ACTION.
        """
        try:
            raw = self.path.read_text("utf-8")
        except FileNotFoundError as exc:
            raise StateError(
                f"找不到狀態檔:{self.path}\n"
                "  這個 app 還沒有安裝好(或這個資料夾不是它的安裝位置)。"
            ) from exc
        except OSError as exc:
            raise StateError(
                f"狀態檔讀不到:{self.path}\n"
                f"  原因:{exc}\n"
                "  常見原因:資料夾是唯讀的、權限不足,或檔案正被其他程式鎖住。"
            ) from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise StateError(
                f"狀態檔內容毀損,不是合法的 JSON:{self.path}\n"
                f"  JSON 解析錯誤:{exc}\n"
                "  這個檔案記錄「目前跑的是哪一版」,壞掉時系統不會亂猜,一律停下來。\n"
                "  請把這個檔案交給開發者(不要自己改),或用 --rollback-to 重新指定版本。"
            ) from exc
        return AppState.from_dict(data, source=self.path)

    def write_locked(self, state: AppState) -> AppState:
        """Atomic replace + read-back. Caller holds the app update lock."""
        new = dataclasses.replace(state, generation=state.generation + 1)
        payload = json.dumps(new.to_dict(), ensure_ascii=False, indent=2)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        verify = self.load()
        if verify.generation != new.generation:
            raise StateError(
                f"post-write verification failed: expected generation {new.generation}, "
                f"read {verify.generation}"
            )
        return verify

    def initialize(self, app_id: str, current: str) -> AppState:
        if self.exists():
            raise StateError(f"state already exists: {self.path}")
        # candidate=current: a fresh install is itself an unproven candidate —
        # its first healthy start commits it as last-known-good, so rollback
        # has a real target from day one.
        state = AppState(app_id=validate_identifier(app_id, "app_id"),
                         current=validate_identifier(current, "current"),
                         candidate=validate_identifier(current, "candidate"),
                         generation=0, last_operation=_op("initialize"))
        with locks_mod.app_lock(self.state_dir):
            return self.write_locked(state)

    def mutate(self, fn) -> AppState:
        """Single-shot locked read-modify-write for callers with no other
        filesystem work inside the critical section."""
        with locks_mod.app_lock(self.state_dir):
            return self.write_locked(fn(self.load()))

"""The shared runtime store: deps/runtimes/<fingerprint>/.

One fingerprint = one immutable CPython + site-packages tree, shared by every
app version whose dependency lock resolves to it. Same lock → zero extra bytes
per release; that is the entire space win.

The fingerprint is computed ONCE, by the builder, and carried as a string ever
after (manifest, runtime.json, folder name). Devices only string-compare —
a second hashing implementation would drift and silently kill runtime reuse
(this repo has been bitten by exactly that before).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

if __package__:
    from . import integrity, locks as locks_mod
    from .identifiers import validate_identifier
else:
    import integrity
    import locks as locks_mod
    from identifiers import validate_identifier

BUILDER_FORMAT_VERSION = 1
RUNTIME_META = "runtime.json"

# name==version, name normalized; extras tolerated.
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?)==([A-Za-z0-9][A-Za-z0-9.+!_-]*)$")
_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _distribution_name(line: str) -> str:
    """The package a requirement line refers to, whatever form it takes
    (`pip==24.1`, `pip @ file:///…`, `pip[extra]>=1`)."""
    match = _NAME.match(line.strip())
    return match.group(1).lower().replace("_", "-") if match else ""


class RuntimeStoreError(Exception):
    pass


class LockfileError(RuntimeStoreError):
    """The dependency lock is not actually a lock (spec §7.1: hard requirement)."""


class SharedComponentError(RuntimeStoreError):
    """A SHARED component on THIS MACHINE is unusable: deps/runtimes/<fp> or
    deps/shells/<fp>.

    The distinction this class exists to make is the difference between a bad
    build and a bad machine. Every version installed here points at the SAME
    deps/runtimes/<fp> and deps/shells/<fp>; a missing or corrupt one is no
    evidence at all against the version that happened to trip over it. Treating
    it as a version-integrity failure (exit 4) marked a perfectly good version
    failed, rolled back to a previous version that then failed in EXACTLY the
    same way, and told the operator we had "restored the previous version" —
    leaving two dead versions and a false story about why.

    bootstrap turns this into EXIT_SHELL_ENVIRONMENT (5): no state written, no
    version blamed, no rollback claimed — and advice() prints what actually fixes
    it. Version-specific breakage (a bad manifest, a files.json mismatch inside
    apps/<app>/versions/<ver>/) is NOT this: it stays a 4.
    """

    def __init__(self, message: str, *, component: str = "runtime",
                 fingerprint: str = "", path: Path | None = None):
        super().__init__(message)
        self.component = component          # "runtime" | "shell"
        self.fingerprint = fingerprint
        self.path = Path(path) if path is not None else None

    # Operator-facing text: Traditional Chinese, cp950-encodable (a zh-TW console
    # cannot print an emoji or a box-drawing character — it raises instead).
    def _what(self) -> str:
        return "Tauri 殼(視窗外殼)" if self.component == "shell" else "Python runtime(共用直譯器)"

    def _location(self) -> str:
        folder = "shells" if self.component == "shell" else "runtimes"
        if self.fingerprint:
            return f"deps\\{folder}\\{self.fingerprint}"
        return str(self.path) if self.path else f"deps\\{folder}"

    def advice(self) -> str:
        raise NotImplementedError


class MissingSharedComponent(SharedComponentError):
    """It is simply not there: never copied, or antivirus quarantined it."""

    def advice(self) -> str:
        return (
            f"這台機器的共用元件不見了:{self._what()}。\n"
            "  共用元件是「每一個版本都在用」的東西,所以這是「這台機器」壞了,不是版本壞了:"
            "退版救不了,舊版會用同一份,一樣起不來。\n"
            "  請照順序做:\n"
            "    1. 請 IT 把整個安裝資料夾加進防毒軟體的排除清單(白名單)。"
            "防毒把共用元件隔離或刪除,是最常見的原因。\n"
            f"    2. 從交付來源把 {self._location()} 整個資料夾原樣複製回來。\n"
            "    3. 如果連視窗都開不起來,請先執行 tools\\安裝WebView2.bat 安裝 WebView2 Runtime。\n"
            "  沒有任何版本被標記為失敗,也沒有退回任何版本。"
        )


class CorruptSharedComponent(SharedComponentError):
    """It is there, but its bytes do not match its own files.json (half-copied
    delivery, a dying disk, an antivirus that "cleaned" a file in place)."""

    def advice(self) -> str:
        return (
            f"這台機器的共用元件壞了:{self._what()}"
            f"(檔案內容和它自己的清單對不起來)。\n"
            "  共用元件是「每一個版本都在用」的東西,所以退版救不了:舊版會用同一份,一樣起不來。\n"
            "  請照順序做:\n"
            f"    1. 刪掉 {self._location()} 整個資料夾,再從交付來源重新複製一份。\n"
            "    2. 重新複製後又壞掉:請 IT 把整個安裝資料夾加進防毒軟體的排除清單(白名單)。\n"
            "    3. 一直驗證失敗:這台機器的磁碟可能有問題,請 IT 檢查(chkdsk)。\n"
            "  沒有任何版本被標記為失敗,也沒有退回任何版本。"
        )


# ── dependency lock normalization ────────────────────────────────────────────

def normalize_lock(text: str) -> list[str]:
    """requirements text -> canonical sorted pins. Anything that is not a plain
    `name==version` pin is rejected: loose specs make the fingerprint a lie."""
    pins: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # Packaging plumbing, not app dependencies: we install and then strip pip
        # ourselves. `pip freeze --all` on a python-build-standalone runtime even
        # emits `pip @ file:///D:/a/python-build-standalone/...`, which would
        # otherwise get rejected as a local-path dependency and block every lock
        # produced the obvious way.
        if _distribution_name(line) in {"pip", "setuptools", "wheel"}:
            continue
        if line.startswith("-"):
            raise LockfileError(f"lock 檔不可含 pip 選項(editable/-r/--hash 等):{line!r}")
        if ";" in line:
            raise LockfileError(f"environment marker 必須在建置時凍結,不可留在 lock 檔:{line!r}")
        if "@" in line or "://" in line:
            raise LockfileError(f"第一版不接受 URL/VCS/local-path 相依:{line!r}")
        match = _PIN.match(line)
        if not match:
            raise LockfileError(
                f"相依必須完全釘死為 name==version,但看到:{line!r}\n"
                "  Store 佈局用「相依指紋」決定 runtime 能不能共用,版本範圍會讓指紋說謊"
                "(今天裝到 1.56、明天裝到 1.58,指紋卻一樣)。\n"
                "  解法:在專案的虛擬環境裡產一份 lock 檔,建置時指向它:\n"
                "      pip freeze > requirements.lock.txt\n"
                "  (或先用「不勾 Store 佈局」的一般模式打包,它接受寬鬆的 requirements。)"
            )
        name, version = match.groups()
        pins.append(f"{name.lower().replace('_', '-')}=={version}")
    if not pins:
        raise LockfileError("lock 檔是空的")
    duplicates = {p.split("==")[0] for p in pins if pins.count(p) > 1}
    pins = sorted(set(pins))
    names = [p.split("==")[0] for p in pins]
    clashes = {n for n in names if names.count(n) > 1} | duplicates
    if clashes:
        raise LockfileError(f"同一套件出現多個版本:{sorted(clashes)}")
    return pins


def compute_fingerprint(*, python_version: str, platform: str, abi: str,
                        pins: list[str],
                        builder_format: int = BUILDER_FORMAT_VERSION) -> str:
    payload = json.dumps(
        {"schema": 1, "python": python_version, "platform": platform, "abi": abi,
         "pins": sorted(pins), "builder_format": builder_format},
        sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{abi}-{digest[:12]}"


# ── the store ────────────────────────────────────────────────────────────────

class ShellStore:
    """The Tauri shell, shared like the runtime.

    It is byte-identical in every version — copying it into each slot made the
    shell 60% of a CV_Viewer version (16.6 MB of 28 MB), and it travelled on
    every single update for nothing.
    """

    def __init__(self, deps_dir: Path):
        self.shells = Path(deps_dir) / "shells"

    def path_for(self, fingerprint: str) -> Path:
        return self.shells / validate_identifier(fingerprint, "shell_fingerprint")

    def exe_for(self, fingerprint: str, name: str) -> Path:
        return self.path_for(fingerprint) / validate_identifier(name, "shell_executable")

    def is_complete(self, fingerprint: str) -> bool:
        return integrity.is_complete(self.path_for(fingerprint))

    def resolve(self, fingerprint: str, name: str) -> Path:
        """The shell to launch, or a loud, actionable error.

        Both failures below are about the MACHINE, not about the version that
        asked for the shell — every version shares this one tree. They are
        therefore SharedComponentError (bootstrap: exit 5, state untouched),
        never a version-integrity failure.
        """
        exe = self.exe_for(fingerprint, name)
        if not exe.is_file():
            raise MissingSharedComponent(
                f"缺共用 Tauri 殼 {fingerprint}:{exe}\n"
                f"  請將交付來源的 deps\\shells\\{fingerprint} 複製到該位置。",
                component="shell", fingerprint=fingerprint,
                path=self.path_for(fingerprint))
        if not integrity.is_complete(self.path_for(fingerprint)):
            problems = integrity.verify_tree(self.path_for(fingerprint))
            if problems:
                raise CorruptSharedComponent(
                    f"Tauri 殼 {fingerprint} 驗證失敗:{problems[:3]}\n"
                    f"  請刪除 {self.path_for(fingerprint)} 後重新複製。",
                    component="shell", fingerprint=fingerprint,
                    path=self.path_for(fingerprint))
            integrity.write_complete(self.path_for(fingerprint))
        return exe


class RuntimeStore:
    def __init__(self, deps_dir: Path):
        self.runtimes = Path(deps_dir) / "runtimes"

    def path_for(self, fingerprint: str) -> Path:
        return self.runtimes / validate_identifier(fingerprint, "runtime_fingerprint")

    def read_meta(self, fingerprint: str) -> dict:
        path = self.path_for(fingerprint) / RUNTIME_META
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CorruptSharedComponent(
                f"runtime.json 不可讀:{path}({exc})",
                component="runtime", fingerprint=fingerprint,
                path=self.path_for(fingerprint)) from exc

    def python_exe(self, fingerprint: str) -> Path:
        return self.path_for(fingerprint) / "python.exe"

    def is_complete(self, fingerprint: str) -> bool:
        root = self.path_for(fingerprint)
        return integrity.is_complete(root) and self.python_exe(fingerprint).is_file()

    def quick_check(self, fingerprint: str) -> None:
        """Cheap per-start gate: sentinel + interpreter + fingerprint identity.

        Everything here is about the SHARED tree (deps/runtimes/<fp>), which no
        single version owns — so every failure is a SharedComponentError and the
        version that asked for it must not be blamed for it.
        """
        root = self.path_for(fingerprint)
        if not root.is_dir():
            raise MissingSharedComponent(
                f"缺共用 runtime {fingerprint}:{root}\n"
                f"  請將更新來源的 deps\\runtimes\\{fingerprint} 複製到該位置。",
                component="runtime", fingerprint=fingerprint, path=root,
            )
        if not self.python_exe(fingerprint).is_file():
            raise MissingSharedComponent(
                f"runtime {fingerprint} 缺 python.exe:{root}",
                component="runtime", fingerprint=fingerprint, path=root)
        recorded = self.read_meta(fingerprint).get("fingerprint")
        if recorded != fingerprint:
            raise CorruptSharedComponent(
                f"runtime 指紋不一致:資料夾 {fingerprint} 但 runtime.json 記錄 {recorded!r}",
                component="runtime", fingerprint=fingerprint, path=root,
            )

    def ensure_verified(self, fingerprint: str, *, progress=None) -> Path:
        """First-start deep verification (spec §10.2): exactly one process does
        the byte-for-byte check, writes .complete, everyone else waits and
        re-checks. Fail closed on any mismatch."""
        root = self.path_for(fingerprint)
        self.quick_check(fingerprint)
        if integrity.is_complete(root):
            return root
        with locks_mod.runtime_lock(self.runtimes, fingerprint):
            if integrity.is_complete(root):  # verified while we waited
                return root
            problems = integrity.verify_tree(root, extra_excluded={RUNTIME_META},
                                             progress=progress)
            if problems:
                head = "\n  ".join(problems[:10])
                raise CorruptSharedComponent(
                    f"runtime {fingerprint} 驗證失敗({len(problems)} 項):\n  {head}\n"
                    f"  請刪除 {root} 後重新從更新來源複製。",
                    component="runtime", fingerprint=fingerprint, path=root,
                )
            integrity.write_complete(root)
        return root

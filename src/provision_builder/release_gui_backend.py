"""Release GUI 的可測試後端（GUI 是它上面的薄殼，不得自帶邏輯）。

原則承襲 gui_backend.py：
- GUI 按鈕背後 = 與 CLI 完全相同的 `release.py` 子程序（GUI 綠 ⇔ CLI 綠）。
- 長時間步驟逐行串流輸出；取消 = taskkill 整棵程序樹（不是旗標式假取消）。
- 所有「畫面要說的話」由後端產生並依事實陳述——對話框不得比結果樂觀。

發布人員旅程 = 本模組的三個入口：
  detect_state()      開場就回答「你現在在哪一步」（金鑰？殼？上次發到哪版？）
  ReleaseRun          每次發版四步 pack → sign → build → verify，一鍵到底
  PromoteRun          internal → production（強制驗章）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_CLI = REPO_ROOT / "release.py"

# 私鑰預設住在使用者家目錄，「工作區之外」——工作區（work/releases）整夾複製上
# USB 是發布人員最自然的動作，金鑰絕不能跟著走（risk 視角一致要求）。
def default_keys_dir() -> Path:
    return Path(os.environ.get("CIM_RELEASE_KEYS", Path.home() / ".cim-keys"))


OnLine = Callable[[str], None]


# ---------------------------------------------------------------------------
# 狀態偵測：開場先回答「你在哪一步」
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkspaceState:
    """發布工作區（keys/work/releases 三個子目錄）的現況——全部是事實，不是猜測。"""

    workspace: Path
    key_file: Path | None            # keys/*.private.json（第一把）
    key_id: str | None
    trust_store: Path | None         # keys/trusted_publishers.json
    platform_root: Path | None       # 找得到 engine.py + prebuilt 殼的平台專案
    shell_exe: Path | None
    last_versions: tuple[str, ...]   # releases/ 內已出過的版本（由 manifest 讀出）
    suggested_version: str           # 最新版 patch+1；沒有歷史 → 1.0.0

    @property
    def keys_ready(self) -> bool:
        return self.key_file is not None and self.trust_store is not None


def _read_key_id(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("key_id")
    except (OSError, ValueError):
        return None


def _released_versions(releases_dir: Path) -> list[str]:
    versions: set[str] = set()
    if not releases_dir.is_dir():
        return []
    for manifest in releases_dir.glob("*/release-manifest.json"):
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for artifact in doc.get("artifacts", []):
            if artifact.get("app_id") == "cim-platform" and artifact.get("version"):
                versions.add(str(artifact["version"]))
    return sorted(versions, key=_version_sort_key)


def _version_sort_key(version: str) -> tuple:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts[:4]) + (version,)


def suggest_next_version(existing: Sequence[str]) -> str:
    """最新版的 patch+1；解析不了或沒有歷史 → 1.0.0。只是建議，欄位可改。"""
    if not existing:
        return "1.0.0"
    latest = max(existing, key=_version_sort_key)
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", latest)
    if not match:
        return "1.0.0"
    major, minor, patch = (int(g) for g in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def find_platform_root(candidates: Sequence[Path | str]) -> tuple[Path | None, Path | None]:
    """(平台根, 殼路徑)。殼可以缺（pack 會給可行動錯誤），engine.py 必須在。"""
    for candidate in candidates:
        root = Path(candidate)
        if (root / "sidecar" / "python-engine" / "engine.py").is_file() or \
                (root / "engine" / "engine.py").is_file():
            shell = root / "apps" / "host-tauri" / "prebuilt" / "cim-light.exe"
            return root, (shell if shell.is_file() else None)
    return None, None


DEFAULT_PLATFORM_CANDIDATES = (Path("C:/code/claude/nativeApp"),)


def detect_state(workspace: Path | str,
                 platform_candidates: Sequence[Path | str] = DEFAULT_PLATFORM_CANDIDATES,
                 keys_dir: Path | str | None = None,
                 ) -> WorkspaceState:
    workspace = Path(workspace)
    keys_dir = Path(keys_dir) if keys_dir else default_keys_dir()
    key_file = next(iter(sorted(keys_dir.glob("*.private.json"))), None) \
        if keys_dir.is_dir() else None
    trust = keys_dir / "trusted_publishers.json"
    versions = _released_versions(workspace / "releases")
    platform_root, shell = find_platform_root(platform_candidates)
    return WorkspaceState(
        workspace=workspace,
        key_file=key_file,
        key_id=_read_key_id(key_file) if key_file else None,
        trust_store=trust if trust.is_file() else None,
        platform_root=platform_root,
        shell_exe=shell,
        last_versions=tuple(versions),
        suggested_version=suggest_next_version(versions),
    )


# ---------------------------------------------------------------------------
# CLI 組裝（每一步 = 一條 release.py 指令；測試直接斷言 argv）
# ---------------------------------------------------------------------------

def _py() -> list[str]:
    return [sys.executable, str(RELEASE_CLI)]


def keygen_command(key_id: str, keys_dir: Path | str | None = None) -> list[str]:
    keys = Path(keys_dir) if keys_dir else default_keys_dir()
    return _py() + ["keygen", "--key-id", key_id,
                    "--out", str(keys / f"{key_id}.private.json"),
                    "--trust-store", str(keys / "trusted_publishers.json")]


@dataclass(frozen=True)
class ReleasePlan:
    """一次發版的全部輸入；由 GUI 欄位組成，先驗證再執行。"""

    workspace: Path
    platform_root: Path
    version: str
    key_file: Path
    trust_store: Path
    channel: str = "internal"
    shell_exe: Path | None = None

    @property
    def napp_path(self) -> Path:
        return self.workspace / "work" / f"cim-platform-{self.version}.napp"

    @property
    def blobstore(self) -> Path:
        return self.workspace / "work" / "blobstore"

    @property
    def releases_dir(self) -> Path:
        return self.workspace / "releases"

    @property
    def release_id(self) -> str:
        return f"{self.channel}-{self.version}"

    @property
    def release_dir(self) -> Path:
        return self.releases_dir / self.release_id

    def problems(self) -> list[str]:
        """執行前把「照著做必失敗」的輸入擋下來，訊息說下一步。"""
        issues: list[str] = []
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.version):
            issues.append(f"版本號要像 1.2.3：{self.version!r}")
        if not (Path(self.platform_root) / "sidecar" / "python-engine" / "engine.py").is_file() \
                and not (Path(self.platform_root) / "engine" / "engine.py").is_file():
            issues.append(f"平台專案沒有 engine.py：{self.platform_root}")
        if not Path(self.key_file).is_file():
            issues.append("找不到私鑰檔——先到「一次性準備」按「產生發行金鑰」")
        if not Path(self.trust_store).is_file():
            issues.append("找不到 trusted_publishers.json——先產生發行金鑰")
        # 簽章相關的錯誤要在這裡（0 秒）就爆，不要等 pack 跑完 62 秒才爆
        if not issues:
            issues += _key_matches_trust(self.key_file, self.trust_store)
        shell = self.shell_exe or (Path(self.platform_root) / "apps" / "host-tauri"
                                   / "prebuilt" / "cim-light.exe")
        if not Path(shell).is_file():
            issues.append("找不到 Tauri 殼（prebuilt\\cim-light.exe）——"
                          "到非 WDAC 機器跑 scripts\\win\\build-shell.bat 後複製就位")
        if self.napp_path.exists():
            issues.append(f"{self.napp_path.name} 已存在——同版本不重打，換版本號（建議欄位旁的值）")
        if self.release_dir.exists():
            issues.append(f"release 目錄已存在：{self.release_dir.name}——release 不就地增補，換版本號")
        return issues

    def partials(self) -> tuple[Path, ...]:
        """失敗/取消時要清掉的「本次才出現」產物（v1 不做續跑，整鏈重跑）。"""
        return (self.napp_path, self.release_dir)

    def steps(self) -> list[tuple[str, list[str]]]:
        pack = _py() + ["pack-platform", str(self.platform_root),
                        "--version", self.version,
                        "--out", str(self.napp_path), "--blobs", str(self.blobstore)]
        if self.shell_exe is not None:
            pack += ["--shell", str(self.shell_exe)]
        return [
            ("打包平台（約 1 分鐘）", pack),
            ("發行者簽章", _py() + ["sign", str(self.napp_path), "--key", str(self.key_file)]),
            ("組 release 資料夾", _py() + ["build", "--out", str(self.releases_dir),
                                          "--napp", str(self.napp_path),
                                          "--blobs", str(self.blobstore),
                                          "--channel", self.channel,
                                          "--release-id", self.release_id,
                                          "--trust-store", str(self.trust_store)]),
            ("出貨前驗證", _py() + ["verify", str(self.release_dir),
                                    "--trust-store", str(self.trust_store)]),
        ]


@dataclass(frozen=True)
class ReleaseInfo:
    """releases/ 底下一個已完成 release 的事實（讀 manifest，不憑記憶）。"""

    release_id: str
    path: Path
    channel: str
    version: str
    promoted_from: str | None


def list_releases(releases_dir: Path | str) -> tuple[ReleaseInfo, ...]:
    """狀態列「上次發版」與晉升下拉的資料源；新 → 舊。"""
    releases_dir = Path(releases_dir)
    found: list[ReleaseInfo] = []
    if not releases_dir.is_dir():
        return ()
    for manifest_path in releases_dir.glob("*/release-manifest.json"):
        try:
            doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            artifact = doc["artifacts"][0]
        except (OSError, ValueError, LookupError):
            continue
        found.append(ReleaseInfo(
            release_id=str(doc.get("release_id", manifest_path.parent.name)),
            path=manifest_path.parent,
            channel=str(doc.get("channel", "")),
            version=str(artifact.get("version", "")),
            promoted_from=doc.get("promoted_from"),
        ))
    found.sort(key=lambda r: _version_sort_key(r.version), reverse=True)
    return tuple(found)


def promotable_releases(releases: Sequence[ReleaseInfo]) -> tuple[ReleaseInfo, ...]:
    """internal 且尚無對應 production-<version> 目錄者。"""
    return tuple(
        release for release in releases
        if release.channel == "internal"
        and not (release.path.parent / f"production-{release.version}").exists()
    )


def verify_command(release_dir: Path | str, trust_store: Path | str) -> list[str]:
    return _py() + ["verify", str(release_dir), "--trust-store", str(trust_store)]


def release_done_note(plan: "ReleasePlan") -> str:
    """步驟 1 成功文案——internal 綠燈**不等於**可交付現場。"""
    return (f"internal 驗證通過：{plan.release_dir}\n"
            "尚不可交付現場——請在下方「晉升 production」完成驗章晉升後再交付。")


def delivery_instructions(production: ReleaseInfo) -> str:
    """步驟 3「複製交付指示」的剪貼簿全文——與畫面同源，避免兩處漂移。"""
    return (
        f"CIM 平台交付指示（{production.release_id}）\n"
        f"1. 把整個資料夾複製到目標機（USB 可；不要經 OneDrive 同步夾）：\n"
        f"   {production.path}\n"
        f"2. 目標機上先驗證再使用：\n"
        f"   py -3.11 release.py verify \"{production.path.name}\"\n"
        f"3. 依資料夾內 RELEASE-REPORT.md 的「離線機使用方式」三步驟安裝。\n"
        f"注意：發佈機的 keys 目錄（私鑰）絕不複製出去。\n"
    )


def _key_matches_trust(key_file: Path | str, trust_store: Path | str) -> list[str]:
    """私鑰能解析、且它的 key_id 在 trust store 裡——不然簽了也驗不過。"""
    try:
        key_doc = json.loads(Path(key_file).read_text(encoding="utf-8"))
        key_id = key_doc["key_id"]
    except (OSError, ValueError, KeyError) as exc:
        return [f"私鑰檔壞了（{exc}）——重新產生一把新 key_id 的金鑰"]
    try:
        trust_doc = json.loads(Path(trust_store).read_text(encoding="utf-8"))
        trusted = {entry.get("key_id") for entry in trust_doc.get("keys", [])}
    except (OSError, ValueError) as exc:
        return [f"trust store 壞了（{exc}）——重新產生金鑰或修復 trusted_publishers.json"]
    if key_id not in trusted:
        return [f"私鑰 key_id={key_id!r} 不在 trust store 裡——"
                "簽了也驗不過；重新產生金鑰（keygen 會一併加入 trust store）"]
    return []


@dataclass(frozen=True)
class PromotePlan:
    source_release: Path
    trust_store: Path
    version: str | None = None       # 只用於命名；None → 從來源 manifest 讀

    def release_id(self) -> str:
        version = self.version or self._source_version() or "unknown"
        return f"production-{version}"

    def _source_version(self) -> str | None:
        try:
            doc = json.loads((Path(self.source_release) / "release-manifest.json")
                             .read_text(encoding="utf-8"))
            return str(doc["artifacts"][0]["version"])
        except (OSError, ValueError, LookupError, KeyError):
            return None

    def problems(self) -> list[str]:
        issues: list[str] = []
        source = Path(self.source_release)
        if not (source / "release-manifest.json").is_file():
            issues.append(f"這不是 release 目錄（缺 release-manifest.json）：{source}")
        if not Path(self.trust_store).is_file():
            issues.append("promote 到 production 必須帶 trust store——先產生發行金鑰")
        if source.is_dir() and (source.parent / self.release_id()).exists():
            issues.append(f"{self.release_id()} 已存在——這一版已經晉升過了")
        return issues

    def steps(self) -> list[tuple[str, list[str]]]:
        source = Path(self.source_release)
        target = source.parent / self.release_id()
        return [
            ("晉升 production（全程重驗＋驗章）",
             _py() + ["promote", str(source), "--to-channel", "production",
                      "--out", str(source.parent), "--release-id", self.release_id(),
                      "--trust-store", str(self.trust_store)]),
            ("驗證 production release",
             _py() + ["verify", str(target), "--trust-store", str(self.trust_store)]),
        ]

    def partials(self) -> tuple[Path, ...]:
        return (Path(self.source_release).parent / self.release_id(),)


# ---------------------------------------------------------------------------
# 執行：逐步跑、逐行報、可取消；結果據實陳述
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    label: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class RunResult:
    steps: list[StepResult] = field(default_factory=list)
    cancelled: bool = False
    removed: tuple[str, ...] = ()   # 失敗/取消後實際清掉的半套產物（事實，不是願望）

    @property
    def ok(self) -> bool:
        return (not self.cancelled) and all(s.ok for s in self.steps)

    def summary(self) -> str:
        """完成對話框的原文——只陳述已發生的事。"""
        cleaned = ("已清除本次未完成的產物：" + "、".join(Path(p).name for p in self.removed) + "。"
                   ) if self.removed else ""
        if self.cancelled:
            done = sum(1 for s in self.steps if s.ok)
            head = (f"已取消。完成 {done} 步、中止於「{self.steps[-1].label}」。"
                    if self.steps else "已取消（尚未開始任何步驟）。")
            return head + cleaned + "同一版本可直接重跑。"
        if self.ok:
            return "全部步驟通過：" + "、".join(s.label for s in self.steps) + "。"
        failed = next(s for s in self.steps if not s.ok)
        return (f"失敗於「{failed.label}」（exit {failed.returncode}）。"
                "上方紀錄的 [FAIL] 行就是原因與下一步。" + cleaned + "修正後可直接重跑同一版本。")


class StepRunner:
    """跑一串 (label, argv)；取消殺整棵樹。執行緒安全供 Tk worker 使用。"""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = threading.Event()

    HEARTBEAT_SECONDS = 15.0

    def run(self, steps: Sequence[tuple[str, Sequence[str]]], on_line: OnLine,
            partials: Sequence[Path | str] = ()) -> RunResult:
        """跑完整鏈；失敗/取消時刪掉 ``partials`` 中「本次才出現」的路徑。

        v1 語意=整鏈重跑：撞名防呆（problems()）因此在重跑時必為空，
        且 summary 只會陳述真的清掉的東西。
        """
        preexisting = {str(p) for p in partials if Path(p).exists()}
        result = RunResult()
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for index, (label, argv) in enumerate(steps, 1):
            if self._cancelled.is_set():
                result.cancelled = True
                break
            on_line(f"[{index}/{len(steps)}] {label}")
            on_line("> " + subprocess.list2cmdline([str(a) for a in argv]))
            self._process = subprocess.Popen(
                [str(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env, creationflags=flags,
            )
            # 心跳：pack 這種一分鐘不吭聲的步驟，沒有它看起來就像當機（repo 舊教訓）。
            step_done = threading.Event()

            def _heartbeat(evt=step_done, lbl=label):
                elapsed = 0
                while not evt.wait(self.HEARTBEAT_SECONDS):
                    elapsed += int(self.HEARTBEAT_SECONDS)
                    on_line(f"  …「{lbl}」仍在執行（已 {elapsed} 秒）")

            ticker = threading.Thread(target=_heartbeat, daemon=True)
            ticker.start()
            assert self._process.stdout is not None
            try:
                for line in self._process.stdout:
                    on_line("  " + line.rstrip("\r\n"))
                code = self._process.wait()
            finally:
                step_done.set()
            if self._cancelled.is_set():
                result.steps.append(StepResult(label, code if code else 1))
                result.cancelled = True
                break
            result.steps.append(StepResult(label, code))
            if code != 0:
                break
        self._process = None
        if not result.ok and partials:
            result.removed = tuple(self._remove_appeared(partials, preexisting, on_line))
        return result

    @staticmethod
    def _remove_appeared(partials: Sequence[Path | str], preexisting: set[str],
                         on_line: OnLine) -> list[str]:
        import shutil

        removed: list[str] = []
        for raw in partials:
            path = Path(raw)
            if str(path) in preexisting or not path.exists():
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed.append(str(path))
                on_line(f"[清理] 已刪除未完成的 {path.name}（同一版本可直接重跑）")
            except OSError as exc:
                on_line(f"[清理] {path.name} 刪不掉（{exc}）——重跑前請手動刪除")
        return removed

    def cancel(self) -> None:
        self._cancelled.set()
        process = self._process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                           check=False)
        else:  # pragma: no cover - 產品目標是 Windows
            process.terminate()

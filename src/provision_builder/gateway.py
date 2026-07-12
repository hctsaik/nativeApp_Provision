r"""與被掃描平台專案的**唯一**耦合點（SPEC D4 / §14）。

所有對平台 `core.deppack` 的呼叫都經過 `PlatformGateway`：把
`<專案>\sidecar\python-engine` 加進 sys.path 後 import 它自己的 `core.deppack`。

為什麼不自己實作產包/驗章：dep-pack 的格式（deppack.json 欄位、requires 指紋演算法、
sha256 定義）必須跟「將要吃這包的那個版本的 engine」完全一致。自己複製一份實作，
平台改格式時本工具不會知道，包產出來到工廠現場才爆。import 它的程式碼 = 格式永遠對齊。

對外一律回 **plain dict / str**（不回平台的 dataclass），讓上層與測試不必也依賴平台型別。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# engine 根相對於平台專案根的位置（平台慣例，見 CLAUDE.md 啟動鏈）。
ENGINE_SUBPATH = ("sidecar", "python-engine")


class GatewayError(Exception):
    """平台專案不合法，或平台 API 呼叫失敗（含 pip download 失敗）。"""


@dataclass(frozen=True)
class Target:
    """目標機器的 wheel 標籤（SPEC D7：一律明示，絕不用本機直譯器的 ABI）。

    歷史事故：某 app 自帶的 wheelhouse 是 cp314（開發機 Python 3.14 的 ABI），
    到鎖 3.11 的平台上 51 個 wheel 全數不可裝。所以這三個值永遠明確傳給 pip。
    """

    platform_tag: str = "win_amd64"
    python_version: str = "3.11"
    abi: str = "cp311"

    @property
    def python_tag(self) -> str:
        """'3.11' → 'cp311'（= deppack manifest 的 python_tag 欄位）。"""
        parts = self.python_version.split(".")
        if len(parts) >= 2:
            return f"cp{parts[0]}{parts[1]}"
        raise GatewayError(f"python_version 格式不正確（需如 '3.11'）：{self.python_version!r}")

    def as_dict(self) -> dict[str, str]:
        return {
            "platform_tag": self.platform_tag,
            "python_version": self.python_version,
            "abi": self.abi,
        }


class PlatformGateway:
    """指向一個 CIM 平台專案根，提供它自己的 deppack API。"""

    def __init__(self, project_root: Path, python_cmd: list[str] | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.engine_root = self.project_root.joinpath(*ENGINE_SUBPATH)
        if not (self.engine_root / "engine.py").is_file():
            # 「build 的第一個參數」是 CLI 的講法；按下按鈕的人看到的是一個標著
            # 「平台專案」的欄位，聽不懂前者，也找不到它。名字要用他螢幕上的那個。
            raise GatewayError(
                f"這不是 CIM 平台專案（找不到 {self.engine_root / 'engine.py'}）。\n"
                f"請把「CIM 平台模組（需 plugin.yaml）」分頁裡的「平台專案」欄位改指到"
                f"平台專案根，例如 C:\\code\\claude\\nativeApp"
                f"（= 底下有 sidecar\\python-engine\\engine.py 的那一層）。\n"
                f"你現在指的是：{self.project_root}\n"
                f"（CLI 的話，就是 build 的第一個參數。）"
            )
        # pip download 用的直譯器。預設 = 跑本工具的直譯器（開發機，應為 3.11）。
        self.python_cmd: list[str] = list(python_cmd) if python_cmd else [sys.executable]
        self._deppack: Any = None

    # ── 平台模組載入 ───────────────────────────────────────────────────────────

    @property
    def deppack(self) -> Any:
        """lazy import 被掃描專案的 core.deppack（第一次呼叫才碰 sys.path）。"""
        if self._deppack is None:
            root = str(self.engine_root)
            if root not in sys.path:
                sys.path.insert(0, root)
            try:
                from core import deppack  # type: ignore[import-not-found]
            except ImportError as exc:
                raise GatewayError(
                    f"無法 import 平台的 core.deppack（{self.engine_root}）：{exc}\n"
                    f"這個平台版本可能太舊（沒有 dep-pack 機制），或 engine 目錄不完整。"
                ) from exc
            self._assert_contract(deppack)
            self._deppack = deppack
        return self._deppack

    @staticmethod
    def _assert_contract(deppack: Any) -> None:
        """守門：平台若改名/移除本工具依賴的 API，這裡立刻爆而不是產出壞包。"""
        required = (
            "build_wheelhouse", "load_manifest", "verify_wheelhouse",
            "verify_deppack_dir", "requires_fingerprint",
            "MANIFEST_FILENAME", "WHEELS_DIRNAME", "DepPackError",
        )
        missing = [name for name in required if not hasattr(deppack, name)]
        if missing:
            raise GatewayError(
                "平台 core.deppack 缺少本工具依賴的 API："
                + "、".join(missing)
                + "。請更新 native_Provision 的 gateway.py（見 SPEC §14 耦合契約）。"
            )

    # ── 常數（來自平台，不寫死）────────────────────────────────────────────────

    @property
    def manifest_filename(self) -> str:
        return str(self.deppack.MANIFEST_FILENAME)

    @property
    def wheels_dirname(self) -> str:
        return str(self.deppack.WHEELS_DIRNAME)

    # ── API 包裝 ───────────────────────────────────────────────────────────────

    def requires_fingerprint(self, requires: list[str]) -> str:
        """平台的 requires 指紋（sha256 of sorted JSON）。裝置端用它確認「這包是給這組 requires 的」。"""
        return str(self.deppack.requires_fingerprint(list(requires)))

    def build_wheelhouse(
        self, tool_id: str, requires: list[str], dest_root: Path, target: Target
    ) -> dict:
        """pip download 一個工具的相依成 <dest_root>/<tool_id>/{wheels/, deppack.json}。

        回 manifest 的 dict。pip 失敗 / requires 空 → GatewayError。
        """
        try:
            manifest = self.deppack.build_wheelhouse(
                tool_id,
                list(requires),
                Path(dest_root),
                python_cmd=self.python_cmd,
                platform_tag=target.platform_tag,
                python_version=target.python_version,
                abi=target.abi,
            )
        except ValueError as exc:  # requires 空
            raise GatewayError(str(exc)) from exc
        except Exception as exc:  # DepPackError（pip download 失敗）等
            raise GatewayError(f"{tool_id}：{exc}") from exc
        return dict(manifest.to_dict())

    def load_manifest(self, manifest_path: Path) -> dict:
        try:
            return dict(self.deppack.load_manifest(Path(manifest_path)).to_dict())
        except Exception as exc:
            raise GatewayError(f"無法解析 {manifest_path}：{exc}") from exc

    def verify_deppack_dir(self, pack_dir: Path) -> tuple[bool, list[str]]:
        """平台原生的「組裝完成後」完整性檢查（wheels/ 對 deppack.json）。

        注意：只適用**大 wheel 已回填**的 pack（= apply 之後的形狀）。
        搬運期 pack 少了 big-deps，要用 verify.verify_pack()（split-aware）。
        """
        ok, errors = self.deppack.verify_deppack_dir(Path(pack_dir))
        return bool(ok), list(errors)

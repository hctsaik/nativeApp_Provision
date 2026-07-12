"""掃描平台專案，找出所有宣告了 `requires:` 的工具（SPEC §7）。

glob **必須鏡射** engine `_scan_and_register_plugins()`：
    scripts/*/plugin.yaml
    plugins/*/modules/*/plugin.yaml
兩邊漂移的後果是「engine 看得到的工具，補給包裡沒有它的 wheel」——到工廠現場才發現。

YAML 讀取：本工具 runtime 不裝 PyYAML（SPEC D2），改用平台直譯器 subprocess 轉 JSON
（SPEC §7.1）。禁止 vendor 或手寫 YAML parser：plugin.yaml 有多行字串與巢狀結構。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# 與 engine._scan_and_register_plugins 完全一致的兩條掃描根。
PLUGIN_GLOBS = ("scripts/*/plugin.yaml", "plugins/*/modules/*/plugin.yaml")

# 批次把 YAML 轉 JSON 的一次性腳本（在平台直譯器裡跑，那邊必有 PyYAML）。
# 路徑經 stdin 傳入，避免 Windows 命令列長度上限。
#
# `ensure_ascii=True`（預設）不是美觀選擇而是正確性要求：子程序的 stdout 在 CP950
# 主控台下是 cp950 編碼，plugin.yaml 的中文 `name:` 會被轉碼再由這裡以 utf-8 解讀
# → 亂碼甚至解析失敗。輸出純 ASCII（\uXXXX 逸出）就與管道編碼完全無關。
_YAML_TO_JSON = """
import json, sys
import yaml
paths = json.loads(sys.stdin.read())
out = {}
for p in paths:
    try:
        with open(p, encoding="utf-8") as fh:
            data = yaml.safe_load(fh.read())
        out[p] = {"ok": True, "data": data if isinstance(data, dict) else {}}
    except Exception as exc:
        out[p] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
sys.stdout.write(json.dumps(out))
"""

# loader 介面：吃 plugin.yaml 路徑清單，回 {str(path): dict}。測試可注入假的。
YamlLoader = Callable[[list[Path]], dict]


class ScanError(Exception):
    """專案結構或 plugin.yaml 有問題，補給包不該在這種狀態下產出。"""


@dataclass(frozen=True)
class ToolSpec:
    """一個宣告了相依的工具。"""

    tool_id: str
    requires: list[str]
    yaml_path: Path


@dataclass
class ScanResult:
    tools: list[ToolSpec] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # {tool_id, reason}

    @property
    def tool_ids(self) -> list[str]:
        return [t.tool_id for t in self.tools]


def make_subprocess_loader(python_cmd: list[str]) -> YamlLoader:
    """回一個「用平台直譯器把 YAML 轉 JSON」的 loader（一次 subprocess 批次處理全部檔案）。"""

    def _load(paths: list[Path]) -> dict:
        if not paths:
            return {}
        payload = json.dumps([str(p) for p in paths])
        try:
            proc = subprocess.run(
                [*python_cmd, "-c", _YAML_TO_JSON],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError) as exc:
            raise ScanError(f"無法執行平台直譯器 {python_cmd!r} 來讀 YAML：{exc}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            hint = ""
            if "No module named 'yaml'" in err or "ModuleNotFoundError" in err:
                hint = (
                    "\n提示：這個直譯器沒有 PyYAML。請用平台環境的 Python 跑本工具"
                    "（例如 py -3.11），或用 --python 指定。"
                )
            raise ScanError(f"讀取 plugin.yaml 失敗：{err}{hint}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ScanError(f"YAML→JSON 轉換輸出不是合法 JSON：{exc}") from exc

    return _load


def find_plugin_yamls(engine_root: Path) -> list[Path]:
    """鏡射 engine 的兩條 glob，順序也一致（scripts 先、plugins 後，各自 sorted）。"""
    engine_root = Path(engine_root)
    found: list[Path] = []
    for pattern in PLUGIN_GLOBS:
        found.extend(sorted(engine_root.glob(pattern)))
    return found


def _normalize_requires(raw: object) -> list[str]:
    """requires 可能是 None / 空 list / 含空字串。統一成乾淨的 str list（保留順序）。"""
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if item is not None and str(item).strip()]


def scan_project(
    engine_root: Path,
    yaml_loader: YamlLoader,
    *,
    only_tools: Iterable[str] | None = None,
) -> ScanResult:
    """掃出所有「enabled 且有 requires」的工具。

    - `enabled: false` → skipped（廢棄模組不佔補給包空間）
    - `requires` 空/缺 → skipped（沒有相依 = 不需要 dep-pack）
    - 同一個 id 出現在兩個資料夾 → **中止**（平台歷史上真的撞過 module_006）
    - plugin.yaml 解析失敗 → **中止**（engine 只會 warning 跳過；但對補給包來說，
      靜默漏掉一個工具的相依 = 工廠現場才發現，寧可在開發機炸掉）
    """
    yaml_paths = find_plugin_yamls(engine_root)
    if not yaml_paths:
        raise ScanError(
            f"在 {engine_root} 下找不到任何 plugin.yaml（掃描根：{', '.join(PLUGIN_GLOBS)}）。"
            f"submodule 可能沒有 clone（試 git submodule update --init --recursive）。"
        )

    loaded = yaml_loader(yaml_paths)

    result = ScanResult()
    seen: dict[str, Path] = {}
    for path in yaml_paths:
        entry = loaded.get(str(path))
        if entry is None:
            raise ScanError(f"YAML loader 沒有回傳 {path} 的結果（內部錯誤）")
        if not entry.get("ok", False):
            raise ScanError(f"plugin.yaml 解析失敗：{path}\n  {entry.get('error', '未知錯誤')}")

        data = entry.get("data") or {}
        tool_id = data.get("id")
        if not tool_id:
            continue  # 與 engine 一致：沒有 id 的 yaml 直接忽略
        tool_id = str(tool_id)

        if tool_id in seen:
            raise ScanError(
                f"工具 id 重複：{tool_id}\n"
                f"  (1) {seen[tool_id]}\n"
                f"  (2) {path}\n"
                f"補給包以 tool_id 為資料夾名，重複會互相覆蓋。請先修正 plugin.yaml。"
            )
        seen[tool_id] = path

        if not data.get("enabled", True):
            result.skipped.append({"tool_id": tool_id, "reason": "disabled（enabled: false）"})
            continue

        requires = _normalize_requires(data.get("requires"))
        if not requires:
            result.skipped.append({"tool_id": tool_id, "reason": "no requires"})
            continue

        result.tools.append(ToolSpec(tool_id=tool_id, requires=requires, yaml_path=path))

    if only_tools is not None:
        wanted = {str(t).strip() for t in only_tools if str(t).strip()}
        unknown = wanted - set(seen)
        if unknown:
            raise ScanError(
                "--tools 指定了掃不到的工具：" + "、".join(sorted(unknown))
                + f"\n（本專案掃到的工具：{'、'.join(sorted(seen)) or '（無）'}）"
            )
        # 指定了但被 skip 的（disabled / 無 requires）→ 明確告知，不是靜默略過
        skipped_ids = {s["tool_id"] for s in result.skipped}
        for tool_id in sorted(wanted & skipped_ids):
            reason = next(s["reason"] for s in result.skipped if s["tool_id"] == tool_id)
            raise ScanError(
                f"--tools 指定的 {tool_id} 不需要補給包（{reason}）。請從 --tools 移除它。"
            )
        result.tools = [t for t in result.tools if t.tool_id in wanted]
        result.skipped = [s for s in result.skipped if s["tool_id"] in wanted]

    return result

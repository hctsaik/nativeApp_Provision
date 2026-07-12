"""report.py — REPORT.md 必含區塊（SPEC §5.2）。"""

from __future__ import annotations

from provision_builder.report import render_report

BASE = {
    "format_version": 1,
    "builder_version": "1.0.0",
    "created_at": "2026-07-10T01:02:03Z",
    "source_project": "C:\\code\\claude\\nativeApp",
    "git": {"platform_commit": "abc123", "submodules": {"plugins/lv": "deadbeef"}},
    "target": {"platform_tag": "win_amd64", "python_version": "3.11", "abi": "cp311"},
    "scanned_roots": ["scripts/*/plugin.yaml"],
    "big_threshold_mb": 100,
    "tools": [{"tool_id": "app-lv", "requires": ["torch==2.6.0", "numpy"],
               "wheel_count": 38, "total_bytes": 900 * 1024 * 1024,
               "big_wheels": ["torch-2.6.0-cp311-cp311-win_amd64.whl"]}],
    "big_deps": [{"name": "torch-2.6.0-cp311-cp311-win_amd64.whl",
                  "sha256": "aa" * 32, "size": 200 * 1024 * 1024, "used_by": ["app-lv"]}],
    "skipped_tools": [{"tool_id": "module_003", "reason": "no requires"}],
    "failed_tools": [],
}
PLAN = {"app-lv": "rebuild"}


def test_overview_section():
    text = render_report(BASE, PLAN)
    assert "# 離線補給包" in text
    assert "C:\\code\\claude\\nativeApp" in text
    assert "abc123" in text
    assert "win_amd64" in text and "3.11" in text and "cp311" in text


def test_big_deps_appear_before_tool_table():
    """大型相依必須醒目且放前面——它決定使用者要不要另外搬運。"""
    text = render_report(BASE, PLAN)
    assert text.index("## 大型相依") < text.index("## 工具清單")
    assert "torch-2.6.0-cp311-cp311-win_amd64.whl" in text
    assert "200.0 MB" in text
    assert "分開搬運" in text
    assert "跳過" in text            # 缺檔行為要先講清楚


def test_tool_table_rows():
    text = render_report(BASE, PLAN)
    assert "| `app-lv` |" in text
    assert "38" in text
    assert "重建" in text


def test_reuse_action_rendered():
    assert "沿用快取" in render_report(BASE, {"app-lv": "reuse"})


def test_skipped_section():
    text = render_report(BASE, PLAN)
    assert "module_003" in text and "no requires" in text


def test_failed_section_is_prominent():
    payload = dict(BASE, failed_tools=[{"tool_id": "bad", "reason": "pip 找不到 ghostpkg"}])
    text = render_report(payload, PLAN)
    assert "產包失敗" in text
    assert "bad" in text and "ghostpkg" in text
    assert "不在" in text            # 明講「這些工具不在包裡」


def test_offline_steps_are_copy_pasteable():
    text = render_report(BASE, PLAN)
    assert "## 在沒有網路的電腦上怎麼用" in text
    assert "apply.py --deppack-cache" in text
    assert "deppack-cache" in text
    assert "project-key" in text                 # 可攜模式的路徑怎麼找
    assert ".deppack-cache" in text              # dev 模式的位置
    assert "CIM_DEPPACK_CACHE" in text
    assert "不執行 pip、不連網" in text
    assert "疑難排解" in text


def test_no_big_deps_says_so():
    payload = dict(BASE, big_deps=[],
                   tools=[dict(BASE["tools"][0], big_wheels=[])])
    text = render_report(payload, PLAN)
    assert "沒有超過門檻的大型相依" in text


def test_pruned_section():
    text = render_report(BASE, PLAN, pruned=["stale-1.0.whl"])
    assert "孤兒大相依" in text and "stale-1.0.whl" in text


def test_submodule_pointers_listed():
    assert "plugins/lv" in render_report(BASE, PLAN)


def test_report_ends_with_newline():
    assert render_report(BASE, PLAN).endswith("\n")

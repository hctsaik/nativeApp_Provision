"""Release GUI 後端（release_gui_backend.py）——multi-agent 裁決規格的驗收測試。

GUI 綠 ⇔ CLI 綠：後端組出的每一步就是一條 release.py 指令。
覆蓋裁決規格 §4 防呆表可機測的項目：撞名/簽章/殼缺 0 秒擋、取消=真取消＋
清半成品＋同版本可重跑、--trust dev 永不露出、internal 綠燈≠可交付、
對話框文字只陳述事實、cp950 中文輸出不炸、交付指示含金鑰警語。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from provision_builder.release_gui_backend import (
    PromotePlan,
    ReleasePlan,
    StepRunner,
    delivery_instructions,
    detect_state,
    keygen_command,
    list_releases,
    promotable_releases,
    release_done_note,
    suggest_next_version,
    verify_command,
)

SHELL_BYTES = b"MZ-fake-shell " * 100


def make_platform(tmp_path: Path, *, with_shell: bool = True, name: str = "platform") -> Path:
    root = tmp_path / name
    engine = root / "sidecar" / "python-engine"
    engine.mkdir(parents=True, exist_ok=True)
    (engine / "engine.py").write_text("MARKER = 'gui'\n", encoding="utf-8")
    shell_dir = root / "apps" / "host-tauri" / "prebuilt"
    shell_dir.mkdir(parents=True, exist_ok=True)
    if with_shell:
        (shell_dir / "cim-light.exe").write_bytes(SHELL_BYTES)
    return root


def make_keys(tmp_path: Path, key_id: str = "fab") -> tuple[Path, Path]:
    """一組真的可用的金鑰（跑真 keygen，保證與 CLI 契約一致）。冪等：已存在就沿用。"""
    keys = tmp_path / "keys"
    key_file = keys / f"{key_id}.private.json"
    if not key_file.is_file():
        result = StepRunner().run([("keygen", keygen_command(key_id, keys_dir=keys))],
                                  lambda _line: None)
        assert result.ok
    return key_file, keys / "trusted_publishers.json"


def run_steps(steps, log=None, partials=()):
    lines: list[str] = []
    result = StepRunner().run(steps, (log or lines.append), partials=partials)
    return result


# ---------------------------------------------------------------------------
# 狀態偵測與版本建議（防呆 #13：狀態不憑記憶）
# ---------------------------------------------------------------------------

def test_detect_state_on_empty_workspace(tmp_path: Path) -> None:
    state = detect_state(tmp_path / "ws", platform_candidates=[tmp_path / "nowhere"],
                         keys_dir=tmp_path / "nokeys")
    assert not state.keys_ready
    assert state.platform_root is None and state.shell_exe is None
    assert state.last_versions == () and state.suggested_version == "1.0.0"


def test_detect_state_sees_keys_platform_and_history(tmp_path: Path) -> None:
    key_file, _trust = make_keys(tmp_path)
    ws = tmp_path / "ws"
    release = ws / "releases" / "internal-1.0.4"
    release.mkdir(parents=True)
    (release / "release-manifest.json").write_text(json.dumps(
        {"release_id": "internal-1.0.4", "channel": "internal",
         "artifacts": [{"app_id": "cim-platform", "version": "1.0.4"}]}), encoding="utf-8")
    platform = make_platform(tmp_path)

    state = detect_state(ws, platform_candidates=[platform], keys_dir=key_file.parent)
    assert state.keys_ready and state.key_id == "fab"
    assert state.platform_root == platform and state.shell_exe is not None
    assert state.last_versions == ("1.0.4",) and state.suggested_version == "1.0.5"


@pytest.mark.parametrize("existing,expected", [
    ([], "1.0.0"),
    (["1.0.0"], "1.0.1"),
    (["1.0.9", "1.0.10"], "1.0.11"),      # 數字排序，不是字串排序
    (["2.1.3", "1.9.9"], "2.1.4"),
    (["weird"], "1.0.0"),                  # 解析不了 → 安全預設
])
def test_suggest_next_version(existing, expected) -> None:
    assert suggest_next_version(existing) == expected


def test_list_and_promotable_releases(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    for release_id, channel, version in [("internal-1.0.0", "internal", "1.0.0"),
                                         ("production-1.0.0", "production", "1.0.0"),
                                         ("internal-1.0.1", "internal", "1.0.1")]:
        d = releases / release_id
        d.mkdir(parents=True)
        (d / "release-manifest.json").write_text(json.dumps(
            {"release_id": release_id, "channel": channel,
             "artifacts": [{"app_id": "cim-platform", "version": version}]}), encoding="utf-8")
    infos = list_releases(releases)
    assert [r.release_id for r in infos][0] == "internal-1.0.1"  # 新 → 舊
    promotable = promotable_releases(infos)
    assert [r.release_id for r in promotable] == ["internal-1.0.1"]  # 1.0.0 已晉升過


# ---------------------------------------------------------------------------
# 輸入防呆（#1 撞名、#2 簽章、#3 殼缺——全部 0 秒擋、訊息可行動）
# ---------------------------------------------------------------------------

def _plan(tmp_path: Path, **overrides) -> ReleasePlan:
    if "key_file" not in overrides:
        key_file, trust = make_keys(tmp_path)
        overrides.setdefault("key_file", key_file)
        overrides.setdefault("trust_store", trust)
    defaults = dict(workspace=tmp_path / "ws", platform_root=make_platform(tmp_path),
                    version="1.0.0")
    defaults.update(overrides)
    return ReleasePlan(**defaults)


def test_plan_happy_path_four_steps_with_trust_store_and_no_dev_trust(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    assert plan.problems() == []
    labels = [label for label, _ in plan.steps()]
    assert len(labels) == 4 and "驗證" in labels[-1]
    argv_flat = [str(a) for _, argv in plan.steps() for a in argv]
    for token in ("pack-platform", "sign", "build", "verify", "--trust-store"):
        assert token in " ".join(argv_flat)
    assert "--trust" not in argv_flat            # 防呆 #6：dev HMAC 永不露出（只准 --trust-store）


def test_plan_blocks_bad_version_and_reused_release(tmp_path: Path) -> None:
    assert any("版本號" in p for p in _plan(tmp_path, version="v1").problems())
    plan = _plan(tmp_path)
    plan.release_dir.mkdir(parents=True)
    assert any("不就地增補" in p for p in plan.problems())
    plan2 = _plan(tmp_path, version="2.0.0")
    plan2.napp_path.parent.mkdir(parents=True, exist_ok=True)
    plan2.napp_path.write_bytes(b"old")
    assert any("已存在" in p for p in plan2.problems())


def test_plan_blocks_signature_problems_before_pack(tmp_path: Path) -> None:
    # 私鑰 JSON 壞掉
    bad_key = tmp_path / "bad.json"
    bad_key.write_text("not json", encoding="utf-8")
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"keys": [{"key_id": "fab"}]}), encoding="utf-8")
    problems = _plan(tmp_path, key_file=bad_key, trust_store=trust).problems()
    assert any("私鑰檔壞了" in p for p in problems)
    # key_id 不在 trust store
    good_key, _ = make_keys(tmp_path, "orphan")
    other_trust = tmp_path / "other-trust.json"
    other_trust.write_text(json.dumps({"keys": [{"key_id": "someone-else"}]}), encoding="utf-8")
    problems = _plan(tmp_path, key_file=good_key, trust_store=other_trust).problems()
    assert any("orphan" in p and "trust store" in p for p in problems)


def test_plan_blocks_missing_shell_with_fix(tmp_path: Path) -> None:
    platform = make_platform(tmp_path, with_shell=False, name="platform-noshell")
    problems = _plan(tmp_path, platform_root=platform).problems()
    assert any("build-shell.bat" in p for p in problems)


def test_promote_plan_guards(tmp_path: Path) -> None:
    plan = PromotePlan(tmp_path / "not-a-release", trust_store=tmp_path / "no.json")
    problems = plan.problems()
    assert any("不是 release 目錄" in p for p in problems)
    assert any("trust store" in p for p in problems)


# ---------------------------------------------------------------------------
# 文案（#8 internal 綠燈 ≠ 可交付、#9 對話框不說謊、#11 交付指示）
# ---------------------------------------------------------------------------

def test_release_done_note_says_not_deliverable(tmp_path: Path) -> None:
    note = release_done_note(_plan(tmp_path))
    assert "尚不可交付" in note and "晉升" in note and "可交付現場" not in note.split("尚不")[0]


def test_delivery_instructions_install_flow_and_keys_warning(tmp_path: Path) -> None:
    from provision_builder.release_gui_backend import ReleaseInfo

    info = ReleaseInfo("production-1.0.0", tmp_path / "production-1.0.0",
                       "production", "1.0.0", "internal-1.0.0")
    text = delivery_instructions(info)
    assert "install.bat" in text                      # 目標機一顆鍵：安裝=更新
    assert "start-platform.bat" in text
    assert "金鑰目錄" in text and "絕不複製" in text   # 私鑰警語
    assert "OneDrive" in text


def test_auto_keygen_prepends_step_and_passes_preflight(tmp_path: Path) -> None:
    """金鑰隱形化：金鑰不存在時不擋發版，改由第一步自動建立。"""
    keys = tmp_path / "fresh-keys"
    plan = _plan(tmp_path, key_file=keys / "fab-team.private.json",
                 trust_store=keys / "trusted_publishers.json", auto_keygen=True)
    assert plan.problems() == []
    steps = plan.steps()
    assert len(steps) == 5 and "自動建立發行金鑰" in steps[0][0]
    # 真的跑：五步全綠（keygen → pack → sign → build → verify）
    result = run_steps(steps, partials=plan.partials())
    assert result.ok
    # 第二次發版：金鑰已在 → 回到四步
    plan2 = _plan(tmp_path, key_file=keys / "fab-team.private.json",
                  trust_store=keys / "trusted_publishers.json",
                  auto_keygen=True, version="1.0.1")
    assert len(plan2.steps()) == 4


def test_summary_tells_facts_for_each_outcome() -> None:
    from provision_builder.release_gui_backend import RunResult, StepResult

    ok = RunResult(steps=[StepResult("打包", 0), StepResult("驗證", 0)])
    assert "全部步驟通過" in ok.summary() and "可交付" not in ok.summary()
    failed = RunResult(steps=[StepResult("打包", 2)])
    assert "失敗於「打包」" in failed.summary()
    cancelled = RunResult(steps=[StepResult("打包", 1)], cancelled=True,
                          removed=("C:\\x\\half.napp",))
    text = cancelled.summary()
    assert "已取消" in text and "half.napp" in text and "重跑" in text


# ---------------------------------------------------------------------------
# StepRunner：真 CLI 全流程（含 promote）、失敗即停、半成品清理、取消殺樹、cp950
# ---------------------------------------------------------------------------

def test_full_release_run_and_promote_via_backend(tmp_path: Path) -> None:
    key_file, trust = make_keys(tmp_path, "fab-team")
    ws = tmp_path / "ws"
    lines: list[str] = []
    state = detect_state(ws, platform_candidates=[make_platform(tmp_path)],
                         keys_dir=key_file.parent)
    assert state.keys_ready

    plan = ReleasePlan(workspace=ws, platform_root=state.platform_root,
                       version="1.0.0", key_file=state.key_file,
                       trust_store=state.trust_store, shell_exe=state.shell_exe)
    assert plan.problems() == []
    result = run_steps(plan.steps(), lines.append, partials=plan.partials())
    assert result.ok, "\n".join(lines[-30:])
    assert result.removed == ()                       # 成功不清任何東西
    assert (plan.release_dir / "RELEASE-REPORT.md").is_file()

    promote = PromotePlan(plan.release_dir, trust_store=state.trust_store, version="1.0.0")
    assert promote.problems() == []
    promoted = run_steps(promote.steps(), lines.append, partials=promote.partials())
    assert promoted.ok, "\n".join(lines[-30:])
    production = plan.releases_dir / "production-1.0.0"
    assert (production / "checksums.sha256").is_file()

    # 狀態不憑記憶：偵測到歷史 → 建議 patch+1；production 已存在 → 不可再晉升
    state2 = detect_state(ws, platform_candidates=[], keys_dir=key_file.parent)
    assert state2.suggested_version == "1.0.1"
    assert promotable_releases(list_releases(ws / "releases")) == ()
    # 單步重驗指令可用
    reverify = run_steps([("重新驗證", verify_command(production, trust))], lines.append)
    assert reverify.ok


def test_failed_step_stops_chain_cleans_partials_and_rerun_is_clean(tmp_path: Path) -> None:
    key_file, trust = make_keys(tmp_path)
    ws = tmp_path / "ws"
    plan = ReleasePlan(workspace=ws, platform_root=tmp_path / "no-platform",
                       version="1.0.0", key_file=key_file, trust_store=trust)
    # 故意繞過 problems() 直接跑：CLI 自己也要擋（GUI 綠 ⇔ CLI 綠）
    result = run_steps(plan.steps(), partials=plan.partials())
    assert not result.ok and len(result.steps) == 1   # 斷在第一步，不續跑
    assert "失敗於「打包平台" in result.summary()
    # 防呆 #4：半成品已清、同版本重跑的防呆檢查為空（除了平台本來就不存在那條）
    assert not plan.napp_path.exists() and not plan.release_dir.exists()
    leftover = [p for p in plan.problems() if "已存在" in p or "不就地增補" in p]
    assert leftover == []


def test_cancel_kills_tree_and_cleans_appeared_partials(tmp_path: Path) -> None:
    runner = StepRunner()
    appeared = tmp_path / "half.napp"
    preexisting = tmp_path / "keep.napp"
    preexisting.write_bytes(b"was here before")
    slow = [("寫檔後睡十秒",
             [sys.executable, "-c",
              f"import pathlib,time; pathlib.Path(r'{appeared}').write_bytes(b'x'); time.sleep(10)"])]
    lines: list[str] = []
    box: dict = {}

    def work():
        box["result"] = runner.run(slow, lines.append, partials=[appeared, preexisting])

    thread = threading.Thread(target=work)
    thread.start()
    time.sleep(2.0)
    runner.cancel()
    thread.join(timeout=15)
    assert not thread.is_alive()
    result = box["result"]
    assert result.cancelled and not result.ok
    assert not appeared.exists()                      # 本次才出現的 → 清掉
    assert preexisting.exists()                       # 本來就在的 → 不碰
    assert "half.napp" in result.summary() and "已取消" in result.summary()


def test_runner_survives_cp950_hostile_output(tmp_path: Path) -> None:
    """防呆 #10：子程序輸出中文/怪字元，StepRunner 不炸、逐行到手。"""
    lines: list[str] = []
    script = "import sys; sys.stdout.buffer.write('中文輸出 ✓\\n'.encode('utf-8'))"
    result = run_steps([("中文輸出", [sys.executable, "-c", script])], lines.append)
    assert result.ok
    assert any("中文輸出" in line for line in lines)
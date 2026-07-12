from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.rollout import (
    AllowAll,
    RoleBasedAuthorizer,
    RolloutService,
    RolloutStore,
    Unauthorized,
    bucket,
)


@pytest.fixture
def svc(tmp_path: Path) -> RolloutService:
    return RolloutService(RolloutStore(tmp_path / "rollout.db"), min_samples=5, failure_threshold=0.2)


DEVICES = [f"device-{i:03d}" for i in range(200)]


def _included(svc: RolloutService, app_id: str, rollout, devices) -> set[str]:
    return {d for d in devices if bucket(app_id, d) < rollout.stage_percent}


def test_bucket_is_deterministic() -> None:
    assert bucket("cv-reviewer", "device-1") == bucket("cv-reviewer", "device-1")
    assert 0 <= bucket("cv-reviewer", "device-1") < 100


def test_staged_rollout_membership_only_grows(svc: RolloutService) -> None:
    r10 = svc.start_rollout("cv-reviewer", "2.0.0", stage_percent=10)
    at10 = _included(svc, "cv-reviewer", r10, DEVICES)
    r50 = svc.advance(r10.rollout_id, 50)
    at50 = _included(svc, "cv-reviewer", r50, DEVICES)
    r100 = svc.advance(r50.rollout_id, 100)
    at100 = _included(svc, "cv-reviewer", r100, DEVICES)
    assert at10 < at50 < at100          # strictly widening on this sample
    assert at100 == set(DEVICES)        # everyone at 100%
    assert r100.status == "completed"


def test_desired_state_splits_by_bucket(svc: RolloutService) -> None:
    svc.start_rollout("cv-reviewer", "2.0.0", stage_percent=50, baseline="1.0.0")
    versions = {svc.desired_for_device("cv-reviewer", d) for d in DEVICES}
    assert versions == {"2.0.0", "1.0.0"}  # some updated, some still baseline


def test_completed_rollout_targets_all_devices(svc: RolloutService) -> None:
    r = svc.start_rollout("cv-reviewer", "2.0.0", stage_percent=10, baseline="1.0.0")
    svc.advance(r.rollout_id, 100)
    assert all(svc.desired_for_device("cv-reviewer", d) == "2.0.0" for d in DEVICES)


def test_failure_rate_auto_pauses(svc: RolloutService) -> None:
    r = svc.start_rollout("cv-reviewer", "2.0.0", stage_percent=50)
    svc.report(r.rollout_id, "d1", True)
    svc.report(r.rollout_id, "d2", True)
    svc.report(r.rollout_id, "d3", False)
    svc.report(r.rollout_id, "d4", False)
    assert svc.get_rollout(r.rollout_id).status == "active"  # 4 samples < min_samples
    after = svc.report(r.rollout_id, "d5", False)            # 3/5 = 60% > 20%
    assert after.status == "paused"
    assert any(e["action"] == "rollout.autopause" for e in svc.audit_log())


def test_healthy_reports_do_not_pause(svc: RolloutService) -> None:
    r = svc.start_rollout("cv-reviewer", "2.0.0", stage_percent=50)
    for i in range(10):
        svc.report(r.rollout_id, f"d{i}", True)
    assert svc.get_rollout(r.rollout_id).status == "active"
    assert svc.failure_rate(r.rollout_id) == 0.0


def test_pause_and_resume(svc: RolloutService) -> None:
    r = svc.start_rollout("cv-reviewer", "2.0.0")
    assert svc.pause(r.rollout_id).status == "paused"
    with pytest.raises(Exception):
        svc.advance(r.rollout_id, 50)  # cannot advance while paused
    assert svc.resume(r.rollout_id).status == "active"


def test_rollout_cannot_narrow(svc: RolloutService) -> None:
    r = svc.start_rollout("cv-reviewer", "2.0.0", stage_percent=50)
    with pytest.raises(Exception):
        svc.advance(r.rollout_id, 25)


def test_rbac_blocks_unauthorized_actor(tmp_path: Path) -> None:
    authz = RoleBasedAuthorizer(
        actor_roles={"alice": {"release-manager"}, "bob": {"viewer"}},
        role_actions={"release-manager": {"rollout.start", "rollout.advance"}, "viewer": set()},
    )
    svc = RolloutService(RolloutStore(tmp_path / "r.db"), authorizer=authz)
    svc.start_rollout("cv-reviewer", "2.0.0", actor="alice")  # allowed
    with pytest.raises(Unauthorized):
        svc.start_rollout("cv-reviewer", "2.0.0", actor="bob")  # denied


def test_audit_records_every_mutation(svc: RolloutService) -> None:
    svc.register_device("d1", "canary")
    r = svc.start_rollout("cv-reviewer", "2.0.0")
    svc.advance(r.rollout_id, 50)
    actions = [e["action"] for e in svc.audit_log()]
    assert actions == ["device.register", "rollout.start", "rollout.advance"]


def test_approval_gate_blocks_wide_rollout(tmp_path: Path) -> None:
    svc = RolloutService(RolloutStore(tmp_path / "r.db"), approval_threshold=10)
    r = svc.start_rollout("cv-reviewer", "2.0.0", stage_percent=10)
    with pytest.raises(Unauthorized):
        svc.advance(r.rollout_id, 50)          # beyond 10% without approval → blocked
    svc.approve(r.rollout_id, actor="release-manager")
    assert svc.advance(r.rollout_id, 50).stage_percent == 50  # approved → allowed
    assert any(e["action"] == "rollout.approve" for e in svc.audit_log())


def test_default_threshold_needs_no_approval(svc: RolloutService) -> None:
    r = svc.start_rollout("cv-reviewer", "2.0.0", stage_percent=10)
    assert svc.advance(r.rollout_id, 100).status == "completed"  # threshold 100 → never blocks


def test_devices_and_groups(svc: RolloutService) -> None:
    svc.register_device("d1", "canary")
    svc.register_device("d2", "canary")
    svc.register_device("d3", "default")
    assert svc.list_devices("canary") == ["d1", "d2"]
    assert set(svc.list_devices()) == {"d1", "d2", "d3"}

# 13 — Phase 0–3 implementation report

Date: 2026-07-12

## Outcome

The platform application model in `12_PLATFORM_APP_MODEL.md` is implemented through the Phase 3 stable endpoint.

- Phase 0: identity, track-selection, and RBAC ADRs; optional launch-hint entrypoint; repeatable WDAC spike.
- Phase 1: Native App Management Center now has Applications and Fleet pages. Applications lists catalog-only and installed apps without app-specific UI code.
- Phase 2: AI4BI imports were audited and ten external requirements are explicitly pinned. `CIM_APPS_ROOT` selects an agent-installed AI4BI source and falls back to the vendor source when unset.
- Phase 3: BuildWorker can resolve a real wheelhouse through PlatformGateway; `.napp` carries dependency metadata and wheels; the Agent verifies hashes, performs `pip --no-index`, writes a completion sentinel, rebuilds poisoned venvs, and reuses matching fingerprints. A directory/USB channel adapter exports releases, artifacts, and blobs.

## Acceptance evidence

- native_Provision full test suite: passed.
- nativeApp engine suite: 785 passed, 1 skipped, 1 expected-pass marker; no failures.
- Real app-lv offline acceptance: 14 declared requirements installed from local wheels; `torch 2.6.0+cpu` imported successfully; source-only 1.0.0 → 1.0.1 update returned `venv_reused=true`.
- Real Tauri/WebView2 Playwright journey: Native App → Management Center → Applications → Fleet → Build → Promote → 10% rollout passed; 11 screenshots captured.
- WDAC development-machine spike: atomic directory replacement and execution of newly landed Python passed. Symlink creation failed with Windows privilege error 1314. The result must still be repeated on the enforced factory WDAC image before production approval.

## Reproduce

```powershell
py -3.11 -m pytest -q
py -3.11 e2e\wdac_spike.py
py -3.11 e2e\phase3_offline_app_lv.py
py -3.11 e2e\capture_native_app_fleet.py
py -3.11 e2e\build_gui_step_guide.py
```

Native App tests:

```powershell
cd C:\code\claude\nativeApp
py -3.11 -m pytest sidecar\python-engine\tests -q
```

## Remaining production gate

Only the environmental WDAC gate remains: execute `e2e/wdac_spike.py` on the actual enforced factory image and archive its policy identity and output. This is not represented as passed by the development-machine result.

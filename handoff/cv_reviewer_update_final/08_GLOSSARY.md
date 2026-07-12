# 08 — 名詞表

| 名詞 | 意義 |
|------|------|
| `.napp` | 有版本、不可變、可驗證的 application package(格式見 `02_ARCHITECTURE.md` §3) |
| `app.yaml` | 應用 repo 內的**宣告**(id、entrypoint、requires…);開發者維護 |
| `package.json` | Build Worker 產出的**建置證明**(commit、fingerprint、hashes…);與 app.yaml 是兩份文件 |
| dep-pack | 平台既有的單一工具離線相依包機制;manifest 權威是 `core.deppack` |
| big-deps | torch 等 2GB 級大 wheel 的隔離機制;新系統中以 content-addressed blob 存放,不內嵌 `.napp` |
| blob / content-addressed | 以內容 SHA-256 為名存放的檔案(`blobs/sha256/<hash>`);同內容天然去重 |
| wheelhouse | 一個工具安裝所需的 wheel 集合資料夾;由 blob hardlink/copy 組裝 |
| dependency fingerprint | 相依集合的雜湊;fingerprint 相同 → 重用既有 venv |
| warmup | `warmup.py`:部署後預建 per-tool venv,避免首次啟動逾時 |
| Registry | 保存 application / release / channel metadata 的 DB(SQLite lab / PostgreSQL 正式) |
| ObjectStore | 保存不可變 artifact 的儲存(filesystem lab / MinIO 正式) |
| release | 一個 `(app_id, version)` 的不可變發布記錄 |
| channel | `dev` / `staging` / `production` 等指標:`(app_id, channel) → version` |
| promote | 把 channel 指標移到某個 published release(指回舊版 = rollback) |
| yank | 撤回 release:標記 `yanked`、不刪 object、裝置停止新安裝 |
| desired state | Control Plane 希望裝置達到的版本(= channel 指標);與「立即安裝命令」相對 |
| observed state | Agent 回報的裝置實際狀態 |
| pointer equality | 判斷是否更新的唯一方式:desired 與 active 的 identity 是否相等;不做 semver 排序 |
| staging(下載) | 下載與解壓的暫存區;驗證通過才進 versions cache |
| atomic activation | 原子切換 active pointer;不存在「一半新一半舊」狀態 |
| LKG(last-known-good) | 最後一個確認健康的版本;晉升條件見 `02_ARCHITECTURE.md` §8 |
| journal / reconcile | Agent 在 state.db 記錄操作進度;開機時據此修復中斷狀態 |
| fail closed | 狀態不明時停在安全側(拒絕/回退),不猜測 |
| contract tests | 同一組測試跑在不同 adapter(SQLite/PG、filesystem/MinIO)上,證明語意一致 |
| error taxonomy | 12 個穩定 domain error(`03_DOMAIN_SPEC.md` §4);HTTP 層只查表映射 |
| orphan / unreferenced object | 上傳成功但 registry 寫入失敗留下的 object;由安全 GC 清理 |
| GC | 只刪「無 release 引用且超過時限」的 object;絕不動有引用者 |
| provenance | 建置出處證明(誰、何時、從哪個 commit、用什麼環境建的) |
| SBOM | Software Bill of Materials,相依清單證明 |
| ADR | Architecture Decision Record;`docs/adr/NNN-*.md`,重大取捨的一頁記錄 |
| WDAC | Windows Defender Application Control;本機擋 cargo 等未簽章原生工具鏈 |
| sidecar | 隨主程式一起部署的獨立輔助程序;本案 Agent = Python sidecar,不進 Rust 殼 |
| Tauri E2E | 用 Playwright 經 CDP 連真實 WebView2 驗證 GUI 的端到端測試(`SPEC.md` §17–18) |
| USB provision | 既有離線部署流程:連網機產包 → USB 搬 → 離線機 apply;必須保持相容 |

# ADR 0001 — Production package signing(P2)

> 狀態:Proposed(待使用者/安全負責人核准)
> 日期:2026-07-11
> 影響:`package.json` / `.napp` 格式(Slice 4)、Build Worker(Slice 5)、
> Native_App Agent 驗簽(Slice 7)。**本 ADR 核准前不得凍結 production package format。**

## 背景

決策 #12:經 Control Plane / MinIO 自動下載執行的 production package **必須驗簽**;
SHA-256 只證完整性,不證發布者身分。既有內部 USB provision 仍照 `SPEC.md` D1,
不強制簽章。因此 `.napp` 需要一個**與雜湊分離**的簽章層,且離線機能在無網路下驗證。

## 決策

1. **演算法:Ed25519**(detached signature)。
   - 理由:小金鑰/簽章、驗證快、無參數陷阱、被廣泛檢視;優於 RSA 的體積與 ECDSA 的隨機數風險。
   - 簽章對象 = `.napp` 的 **canonical digest**:對 `checksums.json.files`(每檔 sha256)
     以 `json.dumps(sort_keys, separators=(",",":"))` 正規化後的 SHA-256(見
     `napp/_layout.py::canonical_digest`)。簽 digest 而非整包,驗證與雜湊解耦。
2. **`signature.json` schema**(格式已由 `napp/signing.py::SignatureBundle` 固定):
   ```json
   {"algorithm":"ed25519","key_id":"<publisher-key-id>",
    "canonical_digest":"<hex sha256>","signature":"<hex/base64>"}
   ```
3. **金鑰與信任根分發**:
   - 每個 publisher 一把 Ed25519 私鑰,存於 Build Worker 可存取的 secret store
     (HSM / KMS / OS keystore),**絕不進 repo、frontend、package**。
   - 裝置端內嵌一份 **trusted public keys**(key_id → public key)離線 trust-store,
     隨 runtime 或 provision 包分發;Agent 只信任其中的 key_id。
4. **輪替與撤銷**:
   - trust-store 支援多把有效公鑰(重疊期),舊 key 標記 `retired` 後停簽但仍可驗舊包。
   - 撤銷 = 從 trust-store 移除該 key_id 並發佈更新;被撤銷 key 簽的版本由 Control Plane
     一併 `yank`(見 §狀態機)。
5. **dev/staging 測試金鑰**:
   - 開發用 `hmac-sha256-dev`(`DevHmacSigner`,對稱,**僅測試**)或一把明確標記
     `key_id="dev"` 的 Ed25519 測試金鑰;裝置 production trust-store **不得**包含它。
6. **Build Worker 取用簽章金鑰**:透過環境注入的 KMS/HSM handle 於簽章步驟即時取用,
   不落地私鑰檔;簽章在隔離 job 內完成後即釋放。

## 現況(本次實作)

- `.napp` 格式、`signature.json` schema、canonical digest、sign/verify 介面**已就緒**
  且以 `DevHmacSigner` 離線測試通過(`tests/test_napp.py`)。
- **待辦(需外部條件)**:Ed25519 signer/verifier 實作需 `cryptography` 或 stdlib 無法提供的
  原生實作;本機 WDAC 下不宜引入含原生碼的第三方套件 → 於 CI/簽章服務端實作,
  介面已預留(新增一組 `Ed25519Signer/Verifier` 即可,格式與呼叫點不變)。

## 後果

- 好:驗證與雜湊解耦、離線可驗、輪替/撤銷有路徑、格式此刻即可凍結(演算法可換不動格式)。
- 代價:需維運一組 publisher 金鑰與裝置 trust-store 分發流程(納入 Slice 8 治理)。

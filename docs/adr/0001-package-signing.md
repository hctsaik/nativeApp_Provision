# ADR 0001 — Production package signing(P2)

> 狀態:**已實作(2026-07-19)**——Ed25519 以純 Python(RFC 8032)落地,見文末「實作紀錄」。
> 金鑰治理(HSM/KMS 分發)仍待安全負責人核准後接上。
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

## 實作紀錄(2026-07-19)——與原假設的一處偏離

原文假設「Ed25519 需要 `cryptography`(原生碼)或 stdlib 無法提供」→ 推遲到 CI 端。
**此假設不成立**:RFC 8032 §6 附完整參考實作,只需 `hashlib.sha512` + 大整數運算,
純 Python 即可——而且**純 Python 恰好避開 WDAC 對原生碼套件的封鎖**,比原方案更適合本環境。
簽章對象是單一 64 字元 digest,毫秒級成本無感。

落點:

| 元件 | 位置 |
|------|------|
| Ed25519 核心(RFC 8032,strict 驗證:拒 s≥L、拒不可解碼點) | `src/provision_builder/napp/ed25519.py` |
| `Ed25519Signer` / `Ed25519Verifier`(§2 schema 不變,`algorithm:"ed25519"`) | `napp/signing.py` |
| trust store(§3/§4:多鑰共存、retired 停簽仍可驗、撤銷=移除)+ keygen + `.napp` 補簽 | `napp/trust.py` |
| CLI:`release.py keygen / sign / build --trust-store / verify / promote` | `release.py` |
| 測試:RFC 8032 §7.1 官方向量 + 可鍛性/翻位元/信任邊界 + promote 全流程 | `tests/test_ed25519.py`、`tests/test_release_signing.py` |

**安全註記(接受的取捨)**:純 Python 非常數時間。驗證端只碰公開資料,無洩漏面;
簽章在建置機以操作者自己的金鑰執行,威脅是金鑰檔失竊而非本機 timing oracle。
**不得**把本模組搬進「對外簽任意資料的網路服務」。金鑰治理(§3 HSM/KMS、§6 Build
Worker 注入)仍是待辦,現階段私鑰檔由發布人員自行保管於 secret store。

## 後果

- 好:驗證與雜湊解耦、離線可驗、輪替/撤銷有路徑、格式此刻即可凍結(演算法可換不動格式)。
- 代價:需維運一組 publisher 金鑰與裝置 trust-store 分發流程(納入 Slice 8 治理)。

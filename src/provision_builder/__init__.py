"""native_Provision — 離線補給包產生器（Provision Builder）。

在有網路的開發機上掃描一個 CIM 平台專案，把所有 plugin 宣告的 Python 相依
（`plugin.yaml` 的 `requires:`）預先下載成離線補給包；複製到沒有網路的電腦後
執行一步 `apply.py`，平台引擎即可全程離線安裝所有工具相依。

規格見 repo 根的 SPEC.md。本套件只在**連網開發機**執行；離線機執行的是自足的
`apply.py`（SPEC D8），它不 import 本套件。
"""

__version__ = "1.0.0"

# provision.json 的格式版本（SPEC §5.1）。欄位語意變更時 +1。
PROVISION_FORMAT_VERSION = 1

# 產出佈局的固定名稱（SPEC §5）。apply.py 有各自的副本（D8 自足性），
# tests/test_apply.py 會驗證兩邊一致。
PACKS_DIRNAME = "packs"
BIG_DEPS_DIRNAME = "big-deps"
PROVISION_MANIFEST = "provision.json"
REPORT_FILENAME = "REPORT.md"

# dep-pack 內部佈局（平台 core.deppack 的常數；gateway 會核對，防漂移）。
DEPPACK_MANIFEST = "deppack.json"
WHEELS_DIRNAME = "wheels"

DEFAULT_BIG_THRESHOLD_MB = 100

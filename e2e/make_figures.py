"""把 GUI E2E 的截圖處理成可內嵌 HTML 的 data URI（縮圖 + JPEG + base64）。

Artifact 的 CSP 擋掉所有外部請求，圖片必須內嵌。原始截圖是 2560×1353 的 PNG，
直接 base64 會讓頁面胖到數 MB；這裡統一縮到 1100px 寬、JPEG q=70，並支援上緣裁切
（portal 的資訊都在畫面上半部）。

用法：py -3.11 e2e/make_figures.py <screenshots目錄> <輸出json>
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

from PIL import Image

TARGET_WIDTH = 1100
QUALITY = 70

# name → (輸出鍵, 從原圖頂端保留的比例；None = 全圖)
# portal 的資訊集中在畫面上緣（工具列 + 狀態），Streamlit 的內容則要留高一點。
WANTED = {
    "C-warmup-first-01-portal.png": ("portal_ready", 0.20),
    "C-warmup-first-02-selected.png": ("tool_selected", 0.20),
    "C-warmup-first-06-final.png": ("warm_rendered", 0.88),
    "B-cold-no-warmup-04-start-failed.png": ("cold_start_failed", 0.34),
    "B-cold-no-warmup-06-final.png": ("cold_rendered_after_retry", 0.88),
    "A-no-provision-06-final.png": ("no_provision_error", 0.60),
}


def encode(path: Path, top_fraction: float | None) -> str:
    with Image.open(path) as img:
        img = img.convert("RGB")
        if top_fraction:
            img = img.crop((0, 0, img.width, int(img.height * top_fraction)))
        if img.width > TARGET_WIDTH:
            height = round(img.height * TARGET_WIDTH / img.width)
            img = img.resize((TARGET_WIDTH, height), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=QUALITY, optimize=True, progressive=True)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{payload}"


def main() -> int:
    shots = Path(sys.argv[1] if len(sys.argv) > 1 else "e2e/out/screenshots")
    dest = Path(sys.argv[2] if len(sys.argv) > 2 else "e2e/out/figures.json")

    figures: dict[str, str] = {}
    for name, (key, top) in WANTED.items():
        path = shots / name
        if not path.is_file():
            print(f"[跳過] 找不到 {path}")
            continue
        figures[key] = encode(path, top)
        kb = len(figures[key]) * 3 // 4 // 1024
        print(f"[OK] {key:22s} ← {name}  ({kb} KB)")

    dest.write_text(json.dumps(figures), encoding="utf-8")
    total = sum(len(v) for v in figures.values()) * 3 // 4 // 1024
    print(f"\n{len(figures)} 張圖，合計約 {total} KB → {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""把設計圖抽出來的家具元件裁短（連通元件把不該綁在一起的東西併成一件時用）。

跑法：python3 tools/trim_props.py      # 冪等，已裁過就跳過

為什麼需要：extract_design.py 依「相連的不透明像素」切元件，於是下排那條長桌
（obj_24）把第 4 組工作站的桌面與桌角那盆植物都併進同一張圖。要把最右邊那組拿掉，
光在 RoomBuilder 的 Hidden 略過 obj_22/23/28/29 是不夠的——桌子本體得跟著縮短，
否則會留下沒有收邊的切口與那盆植物。

切點不是隨便挑的：obj_24 內部 x=145~149 正好是一根隔板，切在它右緣，桌子的
右端就是完整的隔板收邊，看起來像本來就這麼長。

產物與 limezu/ 一樣不進 git（Sprites/LimeZu 已 ignore），換機器重跑本腳本即可。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pnglib import crop, read_png, write_png  # noqa: E402

DESIGN = Path(__file__).parent.parent / "unity/Assets/Sprites/LimeZu/Design"
# name -> 保留的寬度（px）。切點請看檔頭說明，別憑感覺調。
TRIM = {"obj_24": 150}


def main() -> None:
    meta_path = DESIGN / "furniture.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    items = {it["name"]: it for it in data["items"]}
    changed = 0
    for name, keep in TRIM.items():
        it = items.get(name)
        if it is None:
            print(f"⚠ {name} 不在 furniture.json，略過")
            continue
        if it["w"] <= keep:
            print(f"· {name} 已是 {it['w']}px，不用再裁")
            continue
        px = read_png(f"{DESIGN}/{name}.png")
        write_png(f"{DESIGN}/{name}.png", crop(px, 0, 0, keep, len(px)))
        print(f"✓ {name} {it['w']} → {keep}px")
        it["w"] = keep
        changed += 1
    if changed:
        meta_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"furniture.json 已更新（{changed} 件）")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""擦掉家具元件上「不屬於它」的殘留像素。

跑法：python3 tools/clean_props.py      # 冪等，擦過再跑是 no-op

為什麼需要：extract_design.py 依「相連的不透明像素」切元件。右下角那盆植物的葉尖
碰到了下排長桌，於是被併進 obj_24——盆身切成獨立的 obj_29，葉尖卻留在桌子身上。
RoomBuilder 把 obj_29 藏起來（它是右側南北通道唯一的塞子）之後，(14,8) 還會剩一撮
浮在地板上的葉子。obj_24 只有一個連通元件，切不開，只能按座標擦。

素材在 Sprites/LimeZu 底下、不進 git，換機器重跑 extract_design.py 之後要再跑本腳本
一次，否則葉尖會長回來。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pnglib import read_png, write_png  # noqa: E402

DESIGN = Path(__file__).parent.parent / "unity/Assets/Sprites/LimeZu/Design"
# name -> 要清成透明的區域 (x0, y0, x1, y1)，元件內部左上為原點、右下不含。
# obj_24 的桌體到 local x=195 為止（x192~194 是隔板柱），x196 之後只有那撮葉子。
ERASE = {"obj_24": [(196, 27, 210, 32)]}


def main() -> None:
    for name, rects in ERASE.items():
        path = DESIGN / f"{name}.png"
        if not path.exists():
            print(f"⚠ {path} 不存在，略過")
            continue
        px = read_png(str(path))
        wiped = 0
        for x0, y0, x1, y1 in rects:
            for y in range(y0, min(y1, len(px))):
                for x in range(x0, min(x1, len(px[y]))):
                    if px[y][x][3]:
                        px[y][x] = (0, 0, 0, 0)
                        wiped += 1
        if wiped:
            write_png(str(path), px)
            print(f"✓ {name} 擦掉 {wiped} px")
        else:
            print(f"· {name} 已經是乾淨的")


if __name__ == "__main__":
    main()

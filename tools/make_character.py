#!/usr/bin/env python3
"""從 LimeZu 角色 spritesheet 抽 idle/walk 幀成獨立 PNG（Unity 免切格直接吃）。

sheet 格局（16x16 版，官方 GUIDE）：每幀 16×32，
  y=32 列 = idle 24 幀、y=64 列 = walk 24 幀，
  方向順序：down 0-5、up 6-11、right 12-17、left 18-23。

用法：
  python3 tools/make_character.py 17                    # premade 17 號
  python3 tools/make_character.py path/to/sheet.png me  # 任意 sheet（如自組合成的輸出），命名前綴 me

輸出 limezu/_extracted/characters/<prefix>/<prefix>_idle_down_0.png …（共 48 張，衍生物不進 git）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from pnglib import read_png, write_png, crop

PREMADE = ("limezu/Modern_Interiors_v41.4/2_Characters/Character_Generator/"
           "0_Premade_Characters/16x16/Premade_Character_{:02d}.png")
ROWS = {"idle": 32, "walk": 64}
DIRS = [("down", 0), ("up", 6), ("right", 12), ("left", 18)]
SITS = [("sit_right", 128), ("sit_left", 160)]  # 坐姿只有側面兩列，各取前 6 幀
FW, FH, N = 16, 32, 6


def main():
    arg = sys.argv[1]
    if arg.isdigit():
        src, prefix = PREMADE.format(int(arg)), f"p{int(arg):02d}"
    else:
        src, prefix = arg, (sys.argv[2] if len(sys.argv) > 2 else "char")
    out = f"limezu/_extracted/characters/{prefix}"
    os.makedirs(out, exist_ok=True)

    px = read_png(src)
    count = 0
    for anim, y in ROWS.items():
        for dname, start in DIRS:
            for i in range(N):
                frame = crop(px, (start + i) * FW, y, FW, FH)
                write_png(f"{out}/{prefix}_{anim}_{dname}_{i}.png", frame)
                count += 1
    for anim, y in SITS:
        for i in range(N):
            frame = crop(px, i * FW, y, FW, FH)
            write_png(f"{out}/{prefix}_{anim}_{i}.png", frame)
            count += 1
    print(f"{prefix}: {count} 幀 → {out}/")


if __name__ == "__main__":
    main()

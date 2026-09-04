#!/usr/bin/env python3
"""從 LimeZu 角色 spritesheet 抽 idle/walk 幀成獨立 PNG（Unity 免切格直接吃）。

sheet 格局（16x16 版，官方 GUIDE）：每幀 16×32，
  y=32 列 = idle 24 幀、y=64 列 = walk 24 幀，
  方向順序：down 0-5、up 6-11、right 12-17、left 18-23。

用法：
  python3 tools/make_character.py 17                    # premade 17 號
  python3 tools/make_character.py path/to/sheet.png me  # 任意 sheet（如自組合成的輸出），命名前綴 me

輸出 limezu/_extracted/characters/<prefix>/<prefix>_idle_down_0.png …（衍生物不進 git）
幀序號不補零（idle_down_0 … pick_up_left_11）：CharacterBuilder 依序號數值排序，不靠字串序。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from pnglib import read_png, write_png, crop

PREMADE = ("limezu/Modern_Interiors_v41.4/2_Characters/Character_Generator/"
           "0_Premade_Characters/16x16/Premade_Character_{:02d}.png")
ROWS = {"idle": 32, "walk": 64}
# 欄位對應是【量出來的】不是猜的：對 premade sheet 逐欄量膚色像素的左右重心——
#   欄 0-5 面東、欄 6-11 背面、欄 12-17 面西、欄 18-23 正面。
# 原本寫成 down/up/right/left（＝0/6/12/18）是錯的，造成走路方向相反、坐姿也錯。
DIRS = [("right", 0), ("up", 6), ("left", 12), ("down", 18)]
# 坐姿：同一列的欄 0-5 面東、欄 6-11 面西（不是兩列各一個方向——原本兩邊都抓欄 0-5，
# 於是 sit_left 與 sit_right 變成同一個朝向，NPC 坐下必定背對桌子）。
SITS = [("sit_right", 128, 0), ("sit_left", 128, 6)]
# 其餘辦公室用得到的動作（列號同樣是量出來的；官方 GUIDE 標了 phone 的循環段是 4-9）：
#   phone＝拿著手機低頭看（等外部回應）、sleep＝趴著（長時間沒事做）、
#   book＝低頭看書（連續讀檔/查資料的投影；y=224 列只有正面單向，GUIDE 標 1-6 循環）
ACTIONS = [("phone", 192, 4), ("sleep", 96, 0), ("book", 224, 1)]
# 四向的一次性動作：(名稱, 列 y, 每向幀數)。四組依序 right/up/left/down、每組 n 幀，
# 跟 idle/walk 的欄序一致。方向序與幀數都是【量出來的】（膚色重心簽名比對 idle 四向，
# 再把各組首幀拼成對照圖用眼睛確認）——不是看 GUIDE 猜的；GUIDE 沒標方向。
#   gift    遞交（y=320）：4×10 ＋ 2 幀禮盒 prop（不抽）。10 幀是完整的「遞出去」弧線，抽 6 會斷一半
#   hurt    受傷（y=608）：4×3 ＋ 1 格紅色道具（不抽）。三幀＝正常／整身泛紅／正常的閃爍
#   pick_up 撿起（y=288）：4×12　lift 舉起（y=352）：4×14　throw 丟出（y=384）：4×14
# 尚未接線的動作（pick_up／lift／throw）先切進來：貴的是重建 prefab 與 WebGL，一次做完；
# 接線本身是橋端一行（見 backend/main.py 的 hurt_of）。
DIRECTIONAL = [("gift", 320, 10), ("hurt", 608, 3),
               ("pick_up", 288, 12), ("lift", 352, 14), ("throw", 384, 14)]
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
    for anim, y, col in SITS + ACTIONS:
        for i in range(N):
            frame = crop(px, (col + i) * FW, y, FW, FH)
            write_png(f"{out}/{prefix}_{anim}_{i}.png", frame)
            count += 1
    for anim, y, n in DIRECTIONAL:
        for d, (dname, _) in enumerate(DIRS):        # DIRS 已是 right/up/left/down 序
            for i in range(n):
                frame = crop(px, (d * n + i) * FW, y, FW, FH)
                write_png(f"{out}/{prefix}_{anim}_{dname}_{i}.png", frame)
                count += 1
    print(f"{prefix}: {count} 幀 → {out}/")


if __name__ == "__main__":
    main()

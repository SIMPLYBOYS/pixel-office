#!/usr/bin/env python3
"""畫看板（協作模式）的名冊縮圖：backend/avatars/kanban.png

為什麼要一支腳本而不是找一張圖：
- 名冊上其他人的頭像是 LimeZu 角色裁切，那批不進 git（見 .gitignore）。看板不是人、
  沒有對應的角色圖，硬挑一張只會讓人以為它也是員工。
- 純文字 fallback（取名字第一個字「看」）太籠統，看不出那是一塊看板。

所以畫一張【原創】的：三欄看板 + 便利貼，一眼認得出是 kanban 而不是某位同事。
輸出仍放在 backend/avatars/（跟著那個目錄一起被 ignore），這支腳本才是進 git 的那個。

跑法：python3 tools/make_kanban_avatar.py
"""
from pathlib import Path

from PIL import Image

SCALE = 4          # 16×16 邏輯格 → 64×64，與其他頭像同尺寸
GRID = 16

# 調色盤刻意壓深，配 /shell 的深色名冊；便利貼用高彩度才跳得出來。
BG = (0, 0, 0, 0)              # 透明：卡片自己的底色會透出來
FRAME = (58, 48, 38, 255)      # board 外框（木頭深棕）
BOARD = (38, 42, 48, 255)      # board 面（石板灰）
LINE = (72, 78, 86, 255)       # 分欄線
TODO = (232, 196, 84, 255)     # 待辦：黃
DOING = (94, 166, 224, 255)    # 進行中：藍
DONE = (118, 196, 122, 255)    # 完成：綠
SHADOW = (24, 26, 30, 255)     # 便利貼下緣陰影，讓它「貼」在板上

# 16×16 的版面：外框一圈，內部三欄，每欄放不同數量的便利貼——
# 左多右少，讀起來就是「工作從左流到右」。
NOTES = [
    # (欄, 列)：欄 0/1/2 對應 待辦/進行中/完成
    (0, 0), (0, 1), (0, 2),
    (1, 0), (1, 1),
    (2, 0),
]
COL_COLOR = [TODO, DOING, DONE]


def put(px, x, y, color):
    if 0 <= x < GRID and 0 <= y < GRID:
        px[x, y] = color


def main() -> None:
    img = Image.new("RGBA", (GRID, GRID), BG)
    px = img.load()

    # 板身：留 1 格邊界當外框
    for y in range(1, GRID - 1):
        for x in range(1, GRID - 1):
            put(px, x, y, BOARD)
    for x in range(1, GRID - 1):          # 上下框
        put(px, x, 1, FRAME)
        put(px, x, GRID - 2, FRAME)
    for y in range(1, GRID - 1):          # 左右框
        put(px, 1, y, FRAME)
        put(px, GRID - 2, y, FRAME)

    # 欄標題橫槓，不畫直線分欄。
    # 直線在 64px 縮到名冊那個尺寸時會變成三根柱子、比便利貼還搶眼；橫槓則自然讀成「欄標題」，
    # 而且顏色分組本身就把欄分開了。
    col_x = [2, 6, 10]
    for x0 in col_x:
        for dx in range(4):
            put(px, x0 + dx, 3, LINE)

    # 便利貼：每張 4 寬 2 高，往下堆。左多右少＝工作從左流到右。
    for col, row in NOTES:
        x0, y0 = col_x[col], 5 + row * 3
        for dy in range(2):
            for dx in range(4):
                put(px, x0 + dx, y0 + dy, COL_COLOR[col])
        for dx in range(4):               # 下緣陰影：貼紙浮在板上的感覺
            put(px, x0 + dx, y0 + 2, SHADOW)

    out = Path(__file__).resolve().parent.parent / "backend" / "avatars" / "kanban.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    # NEAREST：像素圖放大絕不能內插，糊掉就不是像素風了
    img.resize((GRID * SCALE, GRID * SCALE), Image.NEAREST).save(out)
    print(f"✓ {out}（{GRID * SCALE}×{GRID * SCALE}）")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""從 LimeZu 的 UI 情緒表抽圖示成獨立 PNG（頭上的狀態徽章用）。

sheet 格局是【量出來的】不是照 GUIDE 猜的（角色表那次猜錯方向序，走路整個反過來）：
  UI_thinking_emotes_animation_16x16.png = 160×160 → 10 欄 × 10 列的 16px 格。
  列 0-3 是說明用的示範動畫（點點升起 → 泡泡長大 → 內容），我們不用。
  列 4-9 才是圖示，【每個圖示佔連續兩格】＝兩幀微動畫：
    列 4/5/6 各 5 個（欄 0/2/4/6/8）、列 7/8/9 各 4 個（欄 0/2/4/6），共 27 個。

輸出 limezu/_extracted/emotes/<name>_0.png、_1.png（衍生物不進 git）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from pnglib import read_png, write_png, crop

SRC = ("limezu/Modern_Interiors_v41.4/4_User_Interface_Elements/"
       "UI_thinking_emotes_animation_16x16.png")
OUT = "limezu/_extracted/emotes"
C = 16

# 圖示位置：(列, 起始欄)。列 7-9 只有 4 個，所以不能寫成單純的雙重迴圈。
SLOTS = ([(r, c) for r in (4, 5, 6) for c in (0, 2, 4, 6, 8)]
         + [(r, c) for r in (7, 8, 9) for c in (0, 2, 4, 6)])

# 認得出來的才給語意名，其餘用座標名（e_5_4）。刻意不替每一個都編名字——
# 「破碎的心」「血刀」在辦公室沒有對應的真實狀態，硬取名只會引誘人拿去亂用。
NAMES = {
    (4, 0): "warn",    # 黃色驚嘆號：警戒（額度快滿）
    (5, 0): "alert",   # 紅色驚嘆號：真的被擋下來了
    (4, 6): "think",   # 空白泡泡：正在想（卡住空轉）
    (5, 2): "wait",    # 藍色問號：在等人回答（等審批）
    (6, 2): "ask",     # 黃色問號
    (6, 0): "mail",    # 金色信：訊息
    (6, 4): "idea",    # 黃色寶石：有收穫
    (5, 6): "zzz",     # Z：睡著／閒置
}


# ── 審批倒數計時器 ───────────────────────────────────────────────────────────
# 來源是【另一張】表：UI_16x16.png。同樣是量出來的——
#   綠→綠在列 8-9、綠→紅在列 10-11，每幀 16 寬 × 32 高（跟同名 GIF 尺寸一致）。
# 我們要綠→紅那組：它同時編碼了進度（餅圖填滿）與急迫（轉紅），正好是倒數要講的兩件事。
# 內容只佔格子的 y10-24，所以裁成 16×16——讓它跟其他徽章同尺寸、共用同一個掛點，
# 不必為了它在 Unity 那邊多一組位置。
TIMER_SRC = ("limezu/Modern_Interiors_v41.4/4_User_Interface_Elements/UI_16x16.png")
TIMER_Y = 169     # 綠→紅那組的內容起點（列 10 的 y160 + 內容偏移 9），往上留 1px
TIMER_N = 8


def cut_timer(out: str) -> int:
    px = read_png(TIMER_SRC)
    for i in range(TIMER_N):
        frame = crop(px, i * C, TIMER_Y, C, C)
        if not any(p[3] for line in frame for p in line):
            raise SystemExit(f"倒數第 {i} 幀是空的——UI_16x16 的格局跟預期不符，先重量一次")
        # 檔名 timer_<第幾格>_0：載入端用【最後一個】底線切名字與幀號，
        # 所以每一格各自是一組單幀徽章（由橋決定現在該顯示哪一格）。
        write_png(f"{out}/timer_{i}_0.png", frame)
    return TIMER_N


def main() -> None:
    px = read_png(sys.argv[1] if len(sys.argv) > 1 else SRC)
    os.makedirs(OUT, exist_ok=True)
    n = 0
    for row, col in SLOTS:
        name = NAMES.get((row, col), f"e_{row}_{col}")
        for i in range(2):                      # 兩幀微動畫
            frame = crop(px, (col + i) * C, row * C, C, C)
            if not any(p[3] for line in frame for p in line):
                raise SystemExit(f"格子 ({row},{col}+{i}) 是空的——sheet 格局跟預期不符，"
                                 "先重量一次再改 SLOTS")
            write_png(f"{OUT}/{name}_{i}.png", frame)
            n += 1
    n += cut_timer(OUT)
    print(f"{n} 幀（{len(SLOTS)} 個圖示 + {TIMER_N} 格倒數）→ {OUT}/")


if __name__ == "__main__":
    main()

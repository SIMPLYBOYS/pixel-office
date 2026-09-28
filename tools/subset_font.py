"""泡泡字型子集：從完整的 Noto Sans CJK TC（16MB）切出「常用繁中＋標點＋英數」一份小字型給 Unity。

為什麼：生活模擬的對話是自由文字，以前那份只烘了幾十個狀態詞的圖集字型畫不出來（WebGL 會是空白）；
整份字型塞進 WebGL 又太大。Big5 第一級常用字（5401 字）涵蓋日常對話，切出來約 1–2MB，
Unity 用動態字型（OfficeFontImporter）在執行時才畫字。

用法（tools/get_font.sh 會自動跑）：
  uv run --with fonttools python tools/subset_font.py [完整字型路徑]
"""
import sys
from pathlib import Path

from fontTools import subset

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "unity" / "FontSource" / "NotoSansCJKtc-Regular.otf"
DEST = ROOT / "unity" / "Assets" / "Resources" / "OfficeFont.otf"
# 狀態泡泡原本就在用的符號（字型有字形的那幾個；✔ ✗ ▸ 與 emoji 本來就沒有）
EXTRA = "✓⚠★●▶→「」『』…—～·"


def big5(lo: int, hi: int) -> str:
    """Big5 兩個位元組一格：第二個位元組在 0x40–0x7E 與 0xA1–0xFE。解不開的空格跳過。"""
    out = []
    for code in range(lo, hi + 1):
        b1, b2 = code >> 8, code & 0xFF
        if not (0x40 <= b2 <= 0x7E or 0xA1 <= b2 <= 0xFE):
            continue
        try:
            out.append(bytes([b1, b2]).decode("big5"))
        except UnicodeDecodeError:
            pass
    return "".join(out)


def charset() -> str:
    ascii_ = "".join(chr(c) for c in range(0x20, 0x7F))
    return ascii_ + big5(0xA140, 0xA3BF) + big5(0xA440, 0xC67E) + EXTRA   # 符號區＋第一級常用字


def main() -> None:
    if not SRC.is_file():
        sys.exit(f"找不到完整字型 {SRC}——先跑 tools/get_font.sh")
    text = charset()
    opts = subset.Options()
    opts.hinting = False          # 泡泡字放大縮小都靠 Unity，hinting 只是體積
    opts.layout_features = []     # 不排直書、不做字距微調：泡泡是固定字數硬換行
    opts.name_IDs = ["*"]         # 名稱表整份留著（含授權說明，OFL 要跟著字型走）
    font = subset.load_font(str(SRC), opts)
    sub = subset.Subsetter(opts)
    sub.populate(text=text)
    sub.subset(font)
    DEST.parent.mkdir(parents=True, exist_ok=True)
    subset.save_font(font, str(DEST), opts)
    print(f"✓ {DEST}（{len(set(text))} 字，{DEST.stat().st_size / 1e6:.1f}MB）")


if __name__ == "__main__":
    main()

"""從角色圖抽頭像給 Web 外殼名冊用。

跑法：python3 tools/make_avatars.py
輸入：unity/Assets/Sprites/LimeZu/Characters/<id>/<id>_idle_down_0.png（16×32）
輸出：backend/avatars/<id>.png（64×64，×4 最近鄰放大）

LimeZu 授權紅線：產物與素材一樣不進 git（backend/avatars/ 已 ignore），
換機器重跑本腳本即可。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pnglib  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "unity/Assets/Sprites/LimeZu/Characters"
OUT = ROOT / "backend/avatars"
SIZE = 16   # 裁切邊長（原始像素）
SCALE = 4   # 放大倍率


def head(px: list) -> list:
    """從頭頂往下裁一個正方形：找第一列有不透明像素的位置當頭頂。"""
    top = next((y for y, row in enumerate(px) if any(p[3] for p in row)), 0)
    top = min(top, len(px) - SIZE)
    return pnglib.crop(px, 0, top, min(SIZE, len(px[0])), SIZE)


def upscale(px: list, n: int) -> list:
    return [[p for p in row for _ in range(n)] for row in px for _ in range(n)]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    made = 0
    for d in sorted(SRC.glob("p*")):
        f = d / f"{d.name}_idle_down_0.png"
        if not f.is_file():
            continue
        pnglib.write_png(str(OUT / f"{d.name}.png"), upscale(head(pnglib.read_png(str(f))), SCALE))
        made += 1
        print(f"✓ {d.name}")
    print(f"頭像 {made} 張 → {OUT}" if made else f"找不到角色圖（{SRC}）")


if __name__ == "__main__":
    main()

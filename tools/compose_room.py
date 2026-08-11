#!/usr/bin/env python3
"""程式化拼出【西側新區】（玄關 + 走道 + 會議室）的美術。

為什麼要有這支：辦公區是 extract_design.py 從 LimeZu 官方設計圖拆出來的，但官方沒有
附會議室或大廳的設計圖（Office_Design_1/2 我都量過，一張是現在這間、一張更小）。
所以新區得自己拼。拼的方式刻意跟 extract_design 對齊——同樣輸出「一張底圖 + 每件家具
一個 sprite + 一份座標 json」，RoomBuilder 那邊就不必為新區寫第二套載入邏輯。

輸出（limezu/_extracted/West/，跟其他 LimeZu 衍生物一樣被 .gitignore 蓋住）：
  west_bg.png   — 地板 + 牆
  w_XX.png      — 每件家具
  west.json     — {canvasW, artH, items:[{name,x,y,w,h}]}

⚠ 地板與牆【從辦公區的 bg_base.png 取樣】，不從原始 tilesheet 挑。理由是接縫：兩區在
  OfficeX 那一欄貼在一起，用同一批像素才不會有色差或明暗落差。

用法：python3 tools/compose_room.py
"""
import json
import os
import re
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOM_CS = f"{ROOT}/unity/Assets/Editor/RoomBuilder.cs"
BG_BASE = f"{ROOT}/limezu/_extracted/Office_Design_2/bg_base.png"
OFFICE_SHEET = f"{ROOT}/limezu/Modern_Office_Revamped_v1.2/Modern_Office_16x16.png"
OUT = f"{ROOT}/limezu/_extracted/West"
INSTALL = f"{ROOT}/unity/Assets/Sprites/LimeZu/Design"
CELL = 16


# ── PNG ────────────────────────────────────────────────────────────────────
def read_png(path):
    b = open(path, "rb").read()
    i, idat, w, h, ct = 8, b"", 0, 0, 6
    while i < len(b):
        ln = struct.unpack(">I", b[i:i + 4])[0]
        t = b[i + 4:i + 8]
        if t == b"IHDR":
            w, h = struct.unpack(">II", b[i + 8:i + 16])
            ct = b[i + 17]
        elif t == b"IDAT":
            idat += b[i + 8:i + 8 + ln]
        i += 12 + ln
    raw, ch = zlib.decompress(idat), {6: 4, 2: 3}[ct]
    st, rows, prev, pos = w * ch, [], bytearray(w * ch), 0
    for _ in range(h):
        f = raw[pos]
        L = bytearray(raw[pos + 1:pos + 1 + st])
        pos += 1 + st
        for x in range(st):                       # PNG 五種 filter，逐 byte 還原
            a = L[x - ch] if x >= ch else 0
            up = prev[x]
            ul = prev[x - ch] if x >= ch else 0
            if f == 1:
                L[x] = (L[x] + a) & 255
            elif f == 2:
                L[x] = (L[x] + up) & 255
            elif f == 3:
                L[x] = (L[x] + (a + up) // 2) & 255
            elif f == 4:
                p = a + up - ul
                pa, pb, pc = abs(p - a), abs(p - up), abs(p - ul)
                L[x] = (L[x] + (a if pa <= pb and pa <= pc else up if pb <= pc else ul)) & 255
        rows.append(bytes(L))
        prev = L
    px = [[tuple(rows[y][x * ch:x * ch + ch]) + ((255,) if ch == 3 else ())
           for x in range(w)] for y in range(h)]
    return px


def write_png(path, px):
    H, W = len(px), len(px[0])
    raw = b"".join(b"\x00" + b"".join(struct.pack("4B", *p) for p in row) for row in px)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def sub(px, x, y, w, h):
    return [[px[y + j][x + i] for i in range(w)] for j in range(h)]


def blit(dst, src, x, y):
    for j, row in enumerate(src):
        for i, p in enumerate(row):
            if p[3] > 0 and 0 <= y + j < len(dst) and 0 <= x + i < len(dst[0]):
                dst[y + j][x + i] = p


def crop_opaque(img):
    """裁掉四周全透明的邊，回 (裁過的圖, dx, dy)。家具 sprite 的 pivot 靠底邊對齊，
    留白會讓 Y-sort 的基準線飄掉——所以一定要裁。"""
    ys = [j for j, r in enumerate(img) if any(p[3] > 0 for p in r)]
    xs = [i for i in range(len(img[0])) if any(r[i][3] > 0 for r in img)]
    if not ys or not xs:
        return img, 0, 0
    return ([[img[j][i] for i in range(xs[0], xs[-1] + 1)] for j in range(ys[0], ys[-1] + 1)],
            xs[0], ys[0])


# ── 地圖：唯一真相在 RoomBuilder.cs，這裡用讀的，不另存一份 ────────────────────
def read_west_map():
    src = open(ROOM_CS, encoding="utf-8").read()
    body = re.search(r"static readonly string\[\] West\s*=\s*\{(.*?)\n    \};", src, re.S)
    if not body:
        raise SystemExit("compose_room: RoomBuilder.cs 裡找不到 West 地圖——改過欄位名就要同步這裡")
    rows = re.findall(r'"([#.DT]+)"', body.group(1))
    if len({len(r) for r in rows}) != 1:
        raise SystemExit(f"compose_room: West 各列不等寬 {[len(r) for r in rows]}")
    return rows


def main():
    west = read_west_map()
    CW, CH = len(west[0]), len(west)
    W, H = CW * CELL, CH * CELL
    os.makedirs(OUT, exist_ok=True)

    bg = read_png(BG_BASE)
    sheet = read_png(OFFICE_SHEET)

    # 取樣點（辦公區 bg_base 的像素座標，x=64 那一欄是乾淨的北牆剖面）。
    #
    # 牆【不是】一塊平色，量出來的剖面是：
    #   y0     深藍外框 1px
    #   y1-4   白色頂面 4px      ← 牆的「上表面」，只出現在一段牆的最上緣
    #   y5     深藍 1px
    #   y6-30  淡紫牆面 25px     ← 牆身，可以往下重複
    #   y31    深藍 1px
    #   y32-38 地板陰影 7px      ← 畫在牆【下面那格】的地板上
    # 先前把它當成單一 16×16 平色貼滿，看起來像色塊不像牆——這一段就是為了修那個。
    floor = sub(bg, 4 * CELL, 6 * CELL, CELL, CELL)
    wall_cap = sub(bg, 4 * CELL, 0, CELL, 6)           # 外框 + 白頂 + 外框
    wall_body = sub(bg, 4 * CELL, 6, CELL, CELL)       # 牆身（可重複）
    wall_foot = sub(bg, 4 * CELL, 31, CELL, 1)         # 牆的下緣深藍線
    shadow = sub(bg, 4 * CELL, 32, CELL, 7)            # 牆腳陰影

    canvas = [[(0, 0, 0, 0)] * W for _ in range(H)]
    for r in range(CH):
        for c in range(CW):
            blit(canvas, floor, c * CELL, r * CELL)

    at = lambda r, c: west[r][c] if 0 <= r < CH and 0 <= c < CW else "."
    for r in range(CH):
        for c in range(CW):
            if west[r][c] != "#":
                continue
            x, y = c * CELL, r * CELL
            blit(canvas, wall_body, x, y)
            if at(r - 1, c) != "#":                    # 一段牆的最上緣才有白色頂面
                blit(canvas, wall_cap, x, y)
            if at(r + 1, c) != "#":                    # 最下緣才有暗線與陰影
                blit(canvas, wall_foot, x, y + CELL - 1)
                blit(canvas, shadow, x, y + CELL)
    write_png(f"{OUT}/west_bg.png", canvas)

    # ── 家具 ──────────────────────────────────────────────────────────────
    # (sheet 上的來源格, 尺寸)。行列號是目視點名出來的（tools 裡的 crop 腳本放大看格線）。
    tabletop = sub(sheet, 6 * CELL, 18 * CELL, 2 * CELL, CELL)          # 米色桌面 2×1
    tablelegs = sub(sheet, 6 * CELL, 19 * CELL, 2 * CELL, CELL)         # 桌腳
    # ⚠ 這兩張很容易接反，判準是【椅背在哪一側】：人的背靠著椅背，所以椅背在左＝面向右。
    #   col 5 椅背在左 → 坐的人面東（sit_right），放桌子【西】側
    #   col 4 椅背在右 → 坐的人面西（sit_left），放桌子【東】側
    chair_r = sub(sheet, 5 * CELL, 8 * CELL, CELL, 2 * CELL)            # 給 sit_right 的人
    chair_l = sub(sheet, 4 * CELL, 8 * CELL, CELL, 2 * CELL)            # 給 sit_left 的人
    sofa = sub(sheet, 0 * CELL, 17 * CELL, 2 * CELL, 2 * CELL)          # 雙人沙發
    plant = sub(sheet, 6 * CELL, 8 * CELL, CELL, 2 * CELL)              # 盆栽

    # 長桌：官方沒有 2×3 的直式會議桌，用桌面往下拉長再蓋桌腳。
    # ⚠ 重複的那一段要插在桌面的【下緣線之前】。先前接在整格桌面後面，等於把桌面自己的
    #   下緣暗線留在中間——畫面上就是桌子中央橫過一條假接縫。
    mid = [tabletop[10][:]] * CELL                       # 第 10 列是桌面最平的一段
    table = [r[:] for r in tabletop[:12]] + mid + [r[:] for r in tabletop[12:]] \
        + [r[:] for r in tablelegs]

    items, imgs = [], {}

    def place(name, img, cx, cy):
        """cx,cy = 目標格；sprite 以【底邊】對齊該格底部（同 RoomBuilder 的 pivot 左下）。"""
        img, _, _ = crop_opaque(img)
        h, w = len(img), len(img[0])
        x, y = cx * CELL, (cy + 1) * CELL - h
        imgs[name] = img
        items.append({"name": name, "x": x, "y": y, "w": w, "h": h})

    # 會議室：長桌 x2-3 佔第 11~13 列；座位在 x1(面東) 與 x4(面西)，一邊三個
    place("w_table", table, 2, 13)
    for i, cy in enumerate((11, 12, 13)):
        place(f"w_chair_a{i+1}", chair_r, 1, cy)
        place(f"w_chair_b{i+1}", chair_l, 4, cy)
    # 玄關：接待櫃檯正對自動門、沙發靠北牆、兩盆植物擺角落
    place("w_reception", [r[:] for r in tabletop] + [r[:] for r in tablelegs], 2, 6)
    place("w_sofa", sofa, 2, 3)
    place("w_plant_1", plant, 1, 4)
    place("w_plant_2", plant, 4, 7)

    for name, img in imgs.items():
        write_png(f"{OUT}/{name}.png", img)
    json.dump({"canvasW": W, "artH": H, "items": items},
              open(f"{OUT}/west.json", "w"), indent=1)

    # 直接裝進 Unity 的素材夾。extract_design 那支是「輸出到 _extracted，人再自己複製」，
    # 這支省掉那一步——手動複製漏一個檔的下場是 RoomBuilder 整批放棄建房（它先驗後拆），
    # 錯誤訊息會指向 sprite 名稱，但真正的原因是「你忘了複製」，很難連起來。
    installed = 0
    for fn in os.listdir(OUT):
        if fn.endswith(".meta"):
            continue
        dst = f"{INSTALL}/{fn}"
        if not os.path.isdir(INSTALL):
            print(f"⚠ 找不到 {INSTALL}，跳過安裝——產物仍在 {OUT}/")
            break
        open(dst, "wb").write(open(f"{OUT}/{fn}", "rb").read())
        installed += 1
    print(f"West: {CW}×{CH} 格（{W}×{H}px）、{len(items)} 件家具 → {OUT}/"
          + (f"，已裝 {installed} 個檔到 {INSTALL}/" if installed else ""))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""拆解 LimeZu .aseprite 設計圖 → Unity 用素材。

輸出（預設到 limezu/_extracted/<name>/，該資料夾被 .gitignore 蓋住，
因為產物是 LimeZu 授權素材的衍生物，不能進 git）：
  bg_base.png     — 圖層 0（地板+牆底圖）
  fur_XX.png      — 其餘圖層 frame0 合併後的連通元件（家具，可 Y-sort）
  furniture.json  — 各元件的像素座標 {name,x,y,w,h}，餵給 RoomBuilder.cs 的 Props 表

用法：python3 tools/extract_design.py limezu/Modern_Office_Revamped_v1.2/6_Office_Designs/Office_Design_2.aseprite
"""
import json
import os
import struct
import sys
import zlib


def parse_aseprite(path):
    """回傳 (W, H, cels)；cels = [(frame, layer, x, y, w, h, rgba_bytes)]"""
    d = open(path, 'rb').read()
    _, magic, frames, W, H, depth = struct.unpack('<IHHHHH', d[:14])
    assert magic == 0xA5E0, "不是 aseprite 檔"
    assert depth == 32, f"僅支援 RGBA（depth={depth}）"
    pos = 128
    cels = []
    for f in range(frames):
        fbytes = struct.unpack('<I', d[pos:pos + 4])[0]
        oldn = struct.unpack('<H', d[pos + 6:pos + 8])[0]
        newn = struct.unpack('<I', d[pos + 12:pos + 16])[0]
        n = newn or oldn
        cpos = pos + 16
        for _ in range(n):
            csz, ctype = struct.unpack('<IH', d[cpos:cpos + 6])
            b = cpos + 6
            if ctype == 0x2005:  # cel
                li, x, y, _, typ = struct.unpack('<HhhBH', d[b:b + 9])
                if typ == 2:  # zlib 壓縮影像
                    w, h = struct.unpack('<HH', d[b + 16:b + 20])
                    cels.append((f, li, x, y, w, h, zlib.decompress(d[b + 20:cpos + csz])))
            cpos += csz
        pos += fbytes
    return W, H, cels


def write_png(path, px):
    H, W = len(px), len(px[0])
    raw = b''.join(b'\x00' + b''.join(struct.pack('4B', *p) for p in row) for row in px)
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    open(path, 'wb').write(
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 6, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def main():
    src = sys.argv[1]
    name = os.path.splitext(os.path.basename(src))[0]
    out = sys.argv[2] if len(sys.argv) > 2 else f"limezu/_extracted/{name}"
    os.makedirs(out, exist_ok=True)

    W, H, cels = parse_aseprite(src)

    def layer_canvas(frame, layer):
        cv = [[(0, 0, 0, 0)] * W for _ in range(H)]
        for (f, l, x, y, w, h, raw) in cels:
            if f != frame or l != layer:
                continue
            for yy in range(h):
                for xx in range(w):
                    o = (yy * w + xx) * 4
                    if raw[o + 3] > 0:
                        cv[y + yy][x + xx] = (raw[o], raw[o + 1], raw[o + 2], raw[o + 3])
        return cv

    n_layers = max(c[1] for c in cels) + 1
    base = layer_canvas(0, 0)
    fur = [[(0, 0, 0, 0)] * W for _ in range(H)]
    for li in range(1, n_layers):  # 圖層 1..n 全部壓成家具層（frame 0）
        L = layer_canvas(0, li)
        for y in range(H):
            for x in range(W):
                if L[y][x][3] > 0:
                    fur[y][x] = L[y][x]

    # 移除烙在家具層的原畫人物：從同排無人隔間（bay 週期重複）複製等位像素蓋掉
    # (x0, y0, x1, y1, dx)：dest 矩形 [x0,x1)×[y0,y1) ← 來源同座標平移 dx
    PATCHES = {
        "Office_Design_2": [
            (64, 16, 92, 57, +32),    # 上排 bay1 棕髮筆電男 ← bay2 左區
            (174, 50, 192, 81, -48),  # 上排 bay3 紙堆後紅髮 ← bay2 右區
            (155, 106, 174, 139, -96),  # 下排 bay3 北側紅棕髮 ← bay1 等位
            (136, 118, 161, 161, -48),  # 下排第 9 欄椅上背影（含椅子修復）← bay2/椅6
        ],
    }
    for (px0, py0, px1, py1, pdx) in PATCHES.get(name, []):
        for y in range(py0, py1):
            for x in range(px0, px1):
                fur[y][x] = fur[y][x + pdx]

    ys = [y for y in range(H) for x in range(W) if base[y][x][3] > 0 or fur[y][x][3] > 0]
    H_art = max(ys) + 1
    write_png(f"{out}/bg_base.png", [row[:] for row in base[:H_art]])

    # 家具連通元件（8-鄰接 flood fill）
    lbl = [[0] * W for _ in range(H_art)]
    comps = []
    for y in range(H_art):
        for x in range(W):
            if fur[y][x][3] > 0 and lbl[y][x] == 0:
                stack = [(x, y)]
                lbl[y][x] = len(comps) + 1
                pts = []
                while stack:
                    cx, cy = stack.pop()
                    pts.append((cx, cy))
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            nx, ny = cx + dx, cy + dy
                            if 0 <= nx < W and 0 <= ny < H_art and fur[ny][nx][3] > 0 and lbl[ny][nx] == 0:
                                lbl[ny][nx] = len(comps) + 1
                                stack.append((nx, ny))
                comps.append(pts)

    # Y-sort 物件化：以「角色不可能站立的封鎖列」為全域水平切線，跨線的元件就地切帶，
    # 帶內再跑連通元件 → 每把椅子/每段桌帶都是獨立 sprite，純 Y-sort 天然正確。
    # 按幾何切、不按元件編號——編號會隨內容變動位移，幾何不會
    CUTS = {
        "Office_Design_2": [80, 112, 144],
    }
    cuts_global = CUTS.get(name, [])

    def components_of(pts_set):
        seen = set()
        result = []
        for p in pts_set:
            if p in seen:
                continue
            stack = [p]
            seen.add(p)
            cur = []
            while stack:
                cx, cy = stack.pop()
                cur.append((cx, cy))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        q = (cx + dx, cy + dy)
                        if q in pts_set and q not in seen:
                            seen.add(q)
                            stack.append(q)
            result.append(cur)
        return result

    meta = []
    counter = 0
    for pts in comps:
        y0all = min(p[1] for p in pts)
        y1all = max(p[1] for p in pts)
        cuts = [y0all] + [c for c in cuts_global if y0all < c <= y1all] + [y1all + 1]
        for bi in range(len(cuts) - 1):
            band = {p for p in pts if cuts[bi] <= p[1] < cuts[bi + 1]}
            for sub in sorted(components_of(band), key=lambda c: min(p[0] for p in c)):
                counter += 1
                bname = f"obj_{counter:02d}"
                x0 = min(p[0] for p in sub); x1 = max(p[0] for p in sub)
                y0 = min(p[1] for p in sub); y1 = max(p[1] for p in sub)
                img = [[(0, 0, 0, 0)] * (x1 - x0 + 1) for _ in range(y1 - y0 + 1)]
                for (px_, py_) in sub:
                    img[py_ - y0][px_ - x0] = fur[py_][px_]
                write_png(f"{out}/{bname}.png", img)
                meta.append({"name": bname, "x": x0, "y": y0, "w": x1 - x0 + 1, "h": y1 - y0 + 1})

    json.dump({"canvasW": W, "artH": H_art, "items": meta},
              open(f"{out}/furniture.json", "w"), indent=1)
    print(f"{name}: {H_art}px 高、{len(meta)} 個元件 → {out}/")


if __name__ == "__main__":
    main()

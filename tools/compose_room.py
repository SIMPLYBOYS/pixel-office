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
# 玄關家具直接借 LimeZu 官方 Office_Design_1 拆出來的元件（那張圖的入口廳就是我們要的樣子）。
# 自己從 tilesheet 拼一遍沒有比較好——官方元件已經帶好陰影與細節，而且是同一位作者的手筆。
D1 = f"{ROOT}/limezu/_extracted/Office_Design_1"
DOOR_SHEET = (f"{ROOT}/limezu/Modern_Interiors_v41.4/3_Animated_objects/16x16/"
              "spritesheets/animated_door_sliding_glass.png")
LAYOUT = f"{ROOT}/tools/west_layout.json"
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
def read_map(name):
    src = open(ROOM_CS, encoding="utf-8").read()
    body = re.search(r"static readonly string\[\] " + name + r"\s*=\s*\{(.*?)\n    \};", src, re.S)
    if not body:
        raise SystemExit(f"compose_room: RoomBuilder.cs 裡找不到 {name} 地圖——改過欄位名就要同步這裡")
    rows = re.findall(r'"([#.DT=]+)"', body.group(1))
    if len({len(r) for r in rows}) != 1:
        raise SystemExit(f"compose_room: {name} 各列不等寬 {[len(r) for r in rows]}")
    return rows


def main():
    west = read_map("West")
    office = read_map("OfficeRows")
    CW, CH = len(west[0]), len(west)
    # 底圖往東多畫一格墊在辦公區底下。辦公區底圖在它自己的 x=0 欄第 6~9 列是【全透明】的
    # （原設計圖那裡是建築物外緣，本來就沒畫），以前在畫面邊緣看不出來，西區貼上去之後
    # 那段會變成兩區之間的一條破洞。多畫的這一格由辦公區蓋在上面，只有破洞處會露出來。
    UNDERLAP = 1
    W, H = (CW + UNDERLAP) * CELL, CH * CELL
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
    # 門外那半用【另一種地板】——它在公司外面（大樓公共區），跟室內同色的話「哪裡是門內、
    # 哪裡是門外」就讀不出來，而那正是這個佈局要表達的事。取自 Office_Design_1 的入口廳。
    # 用辦公區【左下房】那種較亮的地磚（平均 189 vs 主辦公區 178，格線也不同）——
    # Design_1 大廳的地板量出來跟主辦公區同色，差別只在格線間距，換過去看不出門內門外。
    outer = sub(bg, 8 * CELL, 14 * CELL, CELL, CELL)
    DOOR_ROW = next(r for r, row in enumerate(west) if "D" in row)
    wall_cap = sub(bg, 4 * CELL, 0, CELL, 6)           # 外框 + 白頂 + 外框
    wall_body = sub(bg, 4 * CELL, 6, CELL, CELL)       # 牆身（可重複）
    wall_foot = sub(bg, 4 * CELL, 31, CELL, 1)         # 牆的下緣深藍線
    shadow = sub(bg, 4 * CELL, 32, CELL, 7)            # 牆腳陰影
    # 立體牆面（'='）＝【外框 + 填滿 + 外框】，結構取自 LimeZu 官方 Gym_2：它的直牆剖面
    # 就是 N+WWWWW+N。所以只要兩種像素：牆頂那片白，跟收邊的深藍。
    wall_top = wall_cap[2][0]                          # 頂面的白
    wall_edge = wall_cap[0][0]                         # 外框的深藍
    if min(wall_top[:3]) < 235 or max(wall_edge[:3]) > 90:
        raise SystemExit("compose_room: 牆頂剖面不是預期的白/深藍——bg_base 換版了，要重新量")

    canvas = [[(0, 0, 0, 0)] * W for _ in range(H)]
    for r in range(CH):
        for c in range(CW + UNDERLAP):
            blit(canvas, outer if r > DOOR_ROW else floor, c * CELL, r * CELL)

    # 墊底欄沿用西區最東欄的牆況：那一欄是走道東牆，續一格才不會有半截牆浮在接縫上
    west = [row + row[-1] * UNDERLAP for row in west]
    CW += UNDERLAP
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

    # 牆（'='）照辦公區 bg_base 自己的配方畫，哪種剖面由【幾何】決定（量它的下排房間）：
    #
    #   房間在牆的【南】側的橫牆 → 立面：頂面 NWWWWN 6px + 牆身 ~25px + 腳線 + 陰影。
    #     這個視角看得到牆的正面（bg_base 列10 那道：NWWWWN + 30px 牆身 + N）。
    #   房間只在【北】側的橫牆   → 只有頂面那條 6px 線（bg_base 南外牆：y=257 一條 NWWWWN）。
    #     牆的正面朝外，從室內只看得到上表面。
    #   直牆                     → N+WWWWW+N 7px 的線（bg_base 兩房之間：x=192 那條）。
    #
    # 線畫在格子的哪個位置：一側是房間就貼著房間（另一側清成透明——建築物外面），
    # 兩側都是房間就置中。
    #
    # ⚠ 位置與剖面都要【整段】一致，不能逐格決定。大門立面是兩列厚的，逐格算的話上下
    #   各畫一條、中間空掉，變成一個空心的帶子。所以先把同方向且相連的格子收成一段
    #   （flood fill），再決定這一段怎麼畫。
    # ⚠ 分段要用【真正的地圖】算，墊底那一欄要排除在外。它只是貼在辦公區底下的複製品，
    #   但它在最東欄右邊也是牆——照單全收的話，東外牆會因為「右邊有牆」被判成橫牆，
    #   於是整片黏成一段（實測：列0–4 與列7–16 各黏成一大塊，線就畫到不該去的地方）。
    CW0 = CW - UNDERLAP
    atw = lambda r, c: west[r][c] if 0 <= r < CH and 0 <= c < CW0 else "."
    horiz = lambda r, c: atw(r, c - 1) == "=" or atw(r, c + 1) == "="
    same = lambda r, c, h: atw(r, c) == "=" and horiz(r, c) == h
    # 東緣【不是】建築物的邊——辦公區就接在那一欄外面。所以往東多算一格是房間，否則東外牆
    # 會朝著辦公區把自己那一側清成透明，接縫上就多一條黑縫。其餘三邊才是真的建築物外緣。
    room = lambda r, c: 0 <= r < CH and 0 <= c <= CW0 and atw(r, c) not in "#="

    walls = [(r, c) for r in range(CH) for c in range(CW0) if west[r][c] == "="]
    seen = set()
    for start in walls:
        if start in seen:
            continue
        h = horiz(*start)                              # 橫牆的線是橫的，直牆的線是直的
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            r, c = stack.pop()
            comp.append((r, c))
            for nb in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if nb not in seen and same(*nb, h):
                    seen.add(nb); stack.append(nb)

        cset = set(comp)
        d = (1, 0) if h else (0, 1)                    # 厚度方向
        neg = any(room(r - d[0], c - d[1]) for r, c in comp)
        pos = any(room(r + d[0], c + d[1]) for r, c in comp)
        if not neg and not pos:
            continue                                   # 整段在建築物外面，什麼都不畫

        # 貼著建築物外面的端點要裁齊：立面／線都只該從直牆的外緣（x=9）起，
        # 不裁的話會比下面的直牆多突出 9px，建築物的西緣就不是一條直線。
        def clip_west(r, c, rows_):
            for j in rows_:
                for i in range(CELL - 7):
                    canvas[r * CELL + j][c * CELL + i] = (0, 0, 0, 0)
                canvas[r * CELL + j][c * CELL + CELL - 7] = wall_edge

        if h and pos:
            # 房間在牆的南側 → 立面。跟 '#' 同一套素材：頂面、牆身、腳線、陰影。
            for r, c in comp:
                x, y = c * CELL, r * CELL
                blit(canvas, wall_body, x, y)
                if (r - 1, c) not in cset:
                    blit(canvas, wall_cap, x, y)
                if (r + 1, c) not in cset:
                    blit(canvas, wall_foot, x, y + CELL - 1)
                    blit(canvas, shadow, x, y + CELL)
                if (r, c - 1) not in cset and not room(r, c - 1) and atw(r, c - 1) != "=":
                    clip_west(r, c, range(CELL))
            continue

        # 其餘只看得到頂面：一條 6px（橫）／7px（直）的線
        proj = lambda rc: rc[0] * d[0] + rc[1] * d[1]
        p0 = min(map(proj, comp)) * CELL
        T = (max(map(proj, comp)) - min(map(proj, comp)) + 1) * CELL
        thick = 6 if h else 7
        off = (T - thick) // 2 if neg and pos else (0 if neg else T - thick)

        for r, c in comp:
            x, y = c * CELL, r * CELL
            for j in range(CELL):
                for i in range(CELL):
                    k = (y + j if h else x + i) - p0 - off
                    if 0 <= k < thick:
                        canvas[y + j][x + i] = wall_edge if k in (0, thick - 1) else wall_top
                    elif not (neg if k < 0 else pos):
                        canvas[y + j][x + i] = (0, 0, 0, 0)   # 線外面沒有房間＝建築物外面
            # 端點：接到房間就用深藍封口（不封的話白色開口對著地板），接到建築物外面就裁齊
            if h:
                for s, xi in ((-1, x), (1, x + CELL - 1)):
                    nb = (r, c + s)
                    if nb in cset or atw(*nb) == "=":
                        continue
                    if room(*nb):
                        for j in range(thick):
                            canvas[p0 + off + j][xi] = wall_edge
                    elif s < 0:
                        j0, j1 = max(0, p0 + off - y), min(CELL, p0 + off + thick - y)
                        if j1 > j0:
                            clip_west(r, c, range(j0, j1))
            else:
                for s, yj in ((-1, y), (1, y + CELL - 1)):
                    nb = (r + s, c)
                    if nb not in cset and atw(*nb) != "=" and room(*nb):
                        for i in range(thick):
                            canvas[yj][p0 + off + i] = wall_edge

    # 墊底欄：把最東欄整欄複製過去。橫牆沿著 x 是均勻的，複製就等於把線延長到接縫底下，
    # 剛好補上辦公區底圖在那裡的破洞；它由辦公區蓋在上面，只有破洞處會露出來。
    for y in range(H):
        for i in range(UNDERLAP):
            canvas[y][(CW0 + i) * CELL:(CW0 + i + 1) * CELL] = canvas[y][(CW0 - 1) * CELL:CW0 * CELL]

    # 大樓的西牆線（兩區之間那條）由墊底欄補畫。bg_base 自己的西牆線只畫到列 9——
    # 它的建築在下半是內縮的，大廳那幾列它什麼都沒有；不補的話大廳的地板直接斷在
    # 建築物外面，東緣沒有任何分界。畫在 bg 同一個位置（bg local x9–15＝墊底欄 +9..+15）、
    # 同一種 N+WWWWW+N 剖面：bg 有畫的列被它蓋住（像素一模一樣），沒畫的列由這裡接手，
    # 上下接成一條。哪幾列該畫看 OfficeRows 的 x0——那裡是牆就有分界，是走道開口就留空。
    for r in range(CH):
        if office[r][0] == "#" and west[r][CW0 - 1] not in "=#":
            for j in range(CELL):
                for i in range(7):
                    canvas[r * CELL + j][CW0 * CELL + 9 + i] = \
                        wall_edge if i in (0, 6) else wall_top
    write_png(f"{OUT}/west_bg.png", canvas)

    # ── 辦公區底圖：切掉它西緣那截被裁斷的牆 ──────────────────────────────
    # bg_base 原本是一張獨立的辦公室設計圖，第 6 列有一道【往西延伸】的牆，在圖的左緣
    # 被裁掉。以前那裡就是畫面邊界，看不出來；西區蓋上去之後，那 10px 變成一截懸空在
    # 走廊上方的白邊，而且比西區的南牆高一列——線就是在這裡對不齊的。
    #
    # 改 LimeZu 素材本身會讓「腳本是唯一真相」破功（那份檔案不進 git，改了沒人知道），
    # 所以在這裡產一份補過的副本，跟著其他產物一起裝進 Design/。來源讀的是
    # _extracted/Office_Design_2，寫的是 OUT/，不會自己吃自己。
    # 兩件事一起裁：往西橫出去的那截（x0–8），以及【往上戳出來的那一列】。辦公區的西牆
    # 原本從列 6 起（它自己的設計在列 5 有開口），比西區的公司南牆高一列，白色牆面就有
    # 一截孤零零戳在走廊上方——那正是看起來多餘的白邊。裁到列 7 就跟南牆同高，白色頂面
    # 從西邊一路過來、在這裡轉南，接成一個 L 而不是斷成兩截。
    ob = [r[:] for r in read_png(BG_BASE)]
    WALL_X, CUT_Y0, TOP_Y = 9, 96, 112               # 牆佔 x9–15；列6.00 裁到列7.00
    got = "".join("W" if min(ob[CUT_Y0 + 1][x][:3]) > 235 else "?" for x in range(WALL_X))
    if got != "W" * WALL_X:
        raise SystemExit(f"compose_room: bg_base 西緣不是預期的白頂（{got}）"
                         "——素材換版了，這段裁切的座標要重新量")
    navy = ob[TOP_Y + 1][WALL_X]                     # 取牆自己的深藍，不寫死顏色
    for y in range(CUT_Y0, TOP_Y):
        for x in range(WALL_X + 7):                  # 只掃這道牆的寬度，別碰同一列右邊的隔間
            ob[y][x] = (0, 0, 0, 0)
    for x in range(WALL_X, WALL_X + 7):
        ob[TOP_Y][x] = navy                          # 牆的上邊框移到列 7，頂面才收得住
    write_png(f"{OUT}/bg_base.png", ob)

    # ── 家具 ────────────────────────────────────────────────────────────
    tabletop = sub(sheet, 6 * CELL, 18 * CELL, 2 * CELL, CELL)
    tablelegs = sub(sheet, 6 * CELL, 19 * CELL, 2 * CELL, CELL)
    # ⚠ 這兩張很容易接反，判準是【椅背在哪一側】：椅背在左＝坐的人面右。
    chair_r = sub(sheet, 5 * CELL, 8 * CELL, CELL, 2 * CELL)   # 給 sit_right 的人（放桌子西側）
    chair_l = sub(sheet, 4 * CELL, 8 * CELL, CELL, 2 * CELL)   # 給 sit_left 的人（放桌子東側）

    # 長桌：官方沒有直式會議桌，用桌面往下拉長再蓋桌腳。重複段要插在桌面【下緣線之前】，
    # 否則桌面自己的暗線會留在中間變成假接縫。
    def widen(img, cells):
        """把桌面橫向拉寬到 cells 格：左右兩端保留原本的邊，中間那一格重複填。
        直接把整張圖並排會出現兩條桌腳／兩道邊，看起來像兩張桌子拼起來。"""
        L = CELL // 2
        band = [row[L:L + 1] for row in img]
        need = cells * CELL - len(img[0])
        return [img[j][:L] + band[j] * need + img[j][L:] for j in range(len(img))]

    top3, legs3 = widen(tabletop, 3), widen(tablelegs, 3)
    mid3 = [top3[10][:]] * CELL
    table = [r[:] for r in top3[:12]] + mid3 + [r[:] for r in top3[12:]] \
        + [r[:] for r in legs3]

    items, imgs = [], {}

    def place(name, img, cx, cy):
        """cx,cy = 目標格；sprite 以【底邊】對齊該格底部（同 RoomBuilder 的 pivot 左下）。"""
        img, _, _ = crop_opaque(img)
        h, w = len(img), len(img[0])
        x, y = cx * CELL, (cy + 1) * CELL - h
        imgs[name] = img
        items.append({"name": name, "x": x, "y": y, "w": w, "h": h})

    # 掛牆的陳設（畫、螢幕）：貼在【牆面】上，不是站在地板上。
    # 先前跟其他家具一樣底邊對齊地板格，畫就變成立在地上——牆是 1 格高，畫要往上推
    # 大半格才會落在牆面。它們也不佔地板：牆上的畫本來就擋不住人。
    NO_BLOCK = set()   # 不佔地板格的（掛牆的陳設、自動門）

    def wall_art(obj, cx, cy):
        """掛在牆面上。cy = 那道牆的【最下面】一列。

        牆的剖面是 6px 頂面 + 26px 牆面（共 2 格），所以畫要貼在牆的下緣往上一點——
        底邊離牆腳 4px。牆若只有一格（16px），畫（20px）比牆還高，怎麼擺都會浮出去；
        那是【地圖】要改成兩列，不是這裡調偏移量能救的。"""
        img, _, _ = crop_opaque(d1_sprite(obj))
        name = "d1_" + obj
        NO_BLOCK.add(name)
        imgs[name] = img
        items.append({"name": name, "x": cx * CELL, "y": (cy + 1) * CELL - 4 - len(img),
                      "w": len(img[0]), "h": len(img)})

    # 烙在設計圖裡的原畫人物要清掉——畫死的人不會動，跟「每個動作都對應真實狀態」牴觸。
    # extract_design.py 對 Office_Design_2 做過同一件事（它的 PATCHES），這裡是 Design_1 的版本。
    # (dest 矩形, 來源往上位移)：拿正上方的等位像素蓋掉，書架本身是縱向重複的圖樣，接得上。
    D1_PATCHES = {"obj_03": (32, 16, 57, 41, 16)}   # 櫃檯後面那位西裝男

    def d1_sprite(obj):
        im = read_png(f"{D1}/{obj}.png")
        if obj in D1_PATCHES:
            x0, y0, x1, y1, dy = D1_PATCHES[obj]
            im = [r[:] for r in im]
            for y in range(y0, y1):
                for x in range(x0, x1):
                    im[y][x] = im[y - dy][x] if 0 <= y - dy < len(im) else (0, 0, 0, 0)
        return im

    def place_px(name, img, x, y):
        """像素級擺放。門外那半的座標是在 Unity 裡手調之後回寫的——格線對齊排出來的
        東西太規矩，手調過的才有「有人住過」的樣子。所以這裡收像素不收格。"""
        img, _, _ = crop_opaque(img)
        imgs[name] = img
        items.append({"name": name, "x": x, "y": y, "w": len(img[0]), "h": len(img)})

    def d1_px(obj, x, y):
        place_px("d1_" + obj, d1_sprite(obj), x, y)

    def d1(obj, cx, cy):
        """借 Office_Design_1 的元件。改前綴 d1_ 是必要的——那張圖的元件也叫 obj_01..，
        跟辦公區的名字會【整批對撞】，sprite 字典與場景物件都會互相蓋掉。"""
        place("d1_" + obj, d1_sprite(obj), cx, cy)

    # 櫃檯（門內左側）：借 Design_1 的【櫃檯叢集】obj_03——檯面、背後書架、桌上的紙、
    # 印表機、椅子全在同一件裡。官方那種「豐富」就是這麼來的：美術師把東西畫在一起，
    # 連通元件掃描把整叢當一件抽出來，天生就有層次與交疊。
    # 它 4.5 格高，上緣會壓在北牆上——那是對的，書架本來就靠牆。碰撞只看底邊那一列。
    d1("obj_03", 1, 4)

    # 會議室（門內右側，獨立一間）：長桌 x8-9 佔第 2~4 列，兩側各三張椅子朝內
    place("w_table", table, 8, 4)
    for i, cy in enumerate((2, 3, 4)):
        place(f"w_chair_a{i+1}", chair_r, 7, cy)
        place(f"w_chair_b{i+1}", chair_l, 11, cy)

    # 門【外】的公共區：大廳 + 靠牆的自助角 + 訪客等候
    wall_art("obj_09", 2, 8)   # 彩色掛畫：掛回大門立面（第 7-8 列）的牆身上
    # 門外的擺設位置來自 tools/west_layout.json——那是在 Unity 裡手調好之後用
    # tools/freeze_layout.py 抽回來的。為什麼不寫死在這裡：Build Room 會 DestroyImmediate
    # 整個 Room 重建，手調的位置下一次就沒了；腳本才是唯一真相，場景是產物。
    # 缺 key 就【當場報錯】，不要靜靜用舊值——「我改了但畫面沒變」大多是這樣來的。
    layout = json.load(open(LAYOUT))
    for name, xy in layout.items():
        if name.startswith("_"):
            continue
        if not name.startswith("d1_"):
            raise SystemExit(f"compose_room: west_layout.json 的 {name} 不是 d1_ 開頭，不知道去哪拿圖")
        d1_px(name[3:], xy[0], xy[1])

    # 自動門：切成獨立的幀，RoomBuilder 收成一個掛 AutoDoor 的物件逐幀播。
    # 只取【前半】——量過幀序是 0=關 →7=全開 →13 又關回去，後半是前半的鏡像，
    # 倒著播就是關門，存 14 張是白存。
    # ⚠ 幀【不裁切】透明邊：每幀的透明區大小不同，各自裁切會讓每張的 pivot 對不齊，
    #   播起來門會左右抖。這也是它不能走 place() 的原因（place 會 crop_opaque）。
    door = read_png(DOOR_SHEET)
    DW = 32
    dcells = [(x, y) for y, row in enumerate(west) for x, c in enumerate(row) if c == "D"]
    if dcells:
        dx, dy = min(c[0] for c in dcells), min(c[1] for c in dcells)
        # +1：14 幀是 0=關 →【7=全開】→13 關回去，取一半只到 6，會少掉全開那張
        for f in range(len(door[0]) // DW // 2 + 1):
            name = f"door_{f}"
            NO_BLOCK.add(name)
            imgs[name] = [row[f * DW:(f + 1) * DW] for row in door]
            items.append({"name": name, "x": dx * CELL, "y": dy * CELL,
                          "w": DW, "h": len(door)})

    # 家具與碰撞圖是分開維護的，對不上就會出現「畫面上有張桌子、NPC 卻走得過去」——
    # 那正是投影不誠實。所以這裡雙向檢查，兩個方向的錯都要吵：
    #
    #   只看【底邊那一列】：俯視角下一件物品佔的是它「站著的那格」。盆栽的葉子、椅背
    #   往上蓋到隔壁格是正常的，那不代表上面那格走不過去。
    #   椅子【反過來】驗：人要坐上去，所以它的格子必須【可走】。標成擋路的話 NPC 走不進
    #   座位，會停在門口等到逾時——而畫面上看起來只是「他沒去開會」。
    seats = {n for n in imgs if "chair" in n}
    # 掛牆的不驗：它掛在牆面上，本來就不該佔地板格
    items_to_check = [it for it in items if it["name"] not in NO_BLOCK]
    bad = []
    for it in items_to_check:
        cy = (it["y"] + it["h"] - 1) // CELL
        for cx in range(it["x"] // CELL, (it["x"] + it["w"] - 1) // CELL + 1):
            ov = min(it["x"] + it["w"], (cx + 1) * CELL) - max(it["x"], cx * CELL)
            if ov <= CELL * 0.45 or not (0 <= cy < CH and 0 <= cx < CW - UNDERLAP):
                continue
            blocked = west[cy][cx] in "#T="       # '=' 是細牆，牆薄不代表走得過去
            if it["name"] in seats and blocked:
                bad.append(f"{it['name']} 的座位 ({cx},{cy}) 被標成擋路，人坐不進去")
            elif it["name"] not in seats and not blocked:
                bad.append(f"{it['name']} 站在 ({cx},{cy})，但那格沒標擋路（'{west[cy][cx]}'）")
    if bad:
        raise SystemExit("compose_room: 家具與 RoomBuilder.cs 的 West 對不上：\n  " + "\n  ".join(bad))

    for name, img in imgs.items():
        write_png(f"{OUT}/{name}.png", img)
    json.dump({"canvasW": W, "artH": H, "items": items},
              open(f"{OUT}/west.json", "w"), indent=1)

    # 直接裝進 Unity 的素材夾。extract_design 那支是「輸出到 _extracted，人再自己複製」，
    # 這支省掉那一步——手動複製漏一個檔的下場是 RoomBuilder 整批放棄建房（它先驗後拆），
    # 錯誤訊息會指向 sprite 名稱，但真正的原因是「你忘了複製」，很難連起來。
    # 先清掉【上一輪產的、這輪不再需要】的檔案。只寫不刪的話，改過佈局之後素材夾裡會同時
    # 存在新舊兩代 sprite，而 Unity 場景裡留著的舊物件看起來一樣正常——「我改了但畫面沒變」
    # 的其中一種來源就是這個。踩過：場景裡還是 w_sofa/w_reception 那一代，而檔案都還在。
    keep = {f"{it['name']}.png" for it in items} | {"west_bg.png", "west.json"}
    for d in (OUT, INSTALL):
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            base = fn[:-5] if fn.endswith(".meta") else fn
            if (base.startswith("w_") or base.startswith("d1_") or base.startswith("west")) \
                    and base not in keep:
                os.remove(f"{d}/{fn}")
                print(f"  清掉舊產物 {d.split('/')[-1]}/{fn}")

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

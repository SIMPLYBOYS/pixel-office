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
    write_png(f"{OUT}/west_bg.png", canvas)

    # ── 家具 ────────────────────────────────────────────────────────────
    tabletop = sub(sheet, 6 * CELL, 18 * CELL, 2 * CELL, CELL)
    tablelegs = sub(sheet, 6 * CELL, 19 * CELL, 2 * CELL, CELL)
    # ⚠ 這兩張很容易接反，判準是【椅背在哪一側】：椅背在左＝坐的人面右。
    chair_r = sub(sheet, 5 * CELL, 8 * CELL, CELL, 2 * CELL)   # 給 sit_right 的人（放桌子西側）
    chair_l = sub(sheet, 4 * CELL, 8 * CELL, CELL, 2 * CELL)   # 給 sit_left 的人（放桌子東側）

    # 長桌：官方沒有直式會議桌，用桌面往下拉長再蓋桌腳。重複段要插在桌面【下緣線之前】，
    # 否則桌面自己的暗線會留在中間變成假接縫。
    mid = [tabletop[10][:]] * CELL
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

    # 掛牆的陳設（畫、螢幕）：貼在【牆面】上，不是站在地板上。
    # 先前跟其他家具一樣底邊對齊地板格，畫就變成立在地上——牆是 1 格高，畫要往上推
    # 大半格才會落在牆面。它們也不佔地板：牆上的畫本來就擋不住人。
    WALL_MOUNTED = set()

    def wall_art(obj, cx, cy):
        """掛在牆面上。cy = 那道牆的【最下面】一列。

        牆的剖面是 6px 頂面 + 26px 牆面（共 2 格），所以畫要貼在牆的下緣往上一點——
        底邊離牆腳 4px。牆若只有一格（16px），畫（20px）比牆還高，怎麼擺都會浮出去；
        那是【地圖】要改成兩列，不是這裡調偏移量能救的。"""
        img, _, _ = crop_opaque(d1_sprite(obj))
        name = "d1_" + obj
        WALL_MOUNTED.add(name)
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
        place(f"w_chair_b{i+1}", chair_l, 10, cy)

    # 門【外】的公共區：大廳 + 靠牆的自助角 + 訪客等候
    wall_art("obj_09", 2, 8)   # 彩色掛畫：掛在公司南牆（第 7-8 列）的牆面上
    d1("obj_12", 2, 10)   # 兩張圓椅（畫下面）
    d1("obj_17", 5, 10)   # 盆栽＋藍椅
    d1("obj_19", 2, 12)   # 藍色候客椅
    d1("obj_20", 3, 12)   # 藍色候客椅
    d1("obj_18", 5, 12)   # 盆栽
    d1("obj_06", 6, 12)   # 盆栽
    # 靠南牆一排（機器都 2 格高，只排一排，不然會上下疊住）
    d1("obj_11", 1, 14)   # 盆栽
    d1("obj_13", 3, 14)   # 販賣機
    d1("obj_14", 6, 14)   # 飲水機
    d1("obj_16", 7, 14)   # 影印機
    d1("obj_05", 8, 14)   # 書架

    # 家具與碰撞圖是分開維護的，對不上就會出現「畫面上有張桌子、NPC 卻走得過去」——
    # 那正是投影不誠實。所以這裡雙向檢查，兩個方向的錯都要吵：
    #
    #   只看【底邊那一列】：俯視角下一件物品佔的是它「站著的那格」。盆栽的葉子、椅背
    #   往上蓋到隔壁格是正常的，那不代表上面那格走不過去。
    #   椅子【反過來】驗：人要坐上去，所以它的格子必須【可走】。標成擋路的話 NPC 走不進
    #   座位，會停在門口等到逾時——而畫面上看起來只是「他沒去開會」。
    seats = {n for n in imgs if "chair" in n}
    # 掛牆的不驗：它掛在牆面上，本來就不該佔地板格
    items_to_check = [it for it in items if it["name"] not in WALL_MOUNTED]
    bad = []
    for it in items_to_check:
        cy = (it["y"] + it["h"] - 1) // CELL
        for cx in range(it["x"] // CELL, (it["x"] + it["w"] - 1) // CELL + 1):
            ov = min(it["x"] + it["w"], (cx + 1) * CELL) - max(it["x"], cx * CELL)
            if ov <= CELL * 0.45 or not (0 <= cy < CH and 0 <= cx < CW - UNDERLAP):
                continue
            blocked = west[cy][cx] in "#T"
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

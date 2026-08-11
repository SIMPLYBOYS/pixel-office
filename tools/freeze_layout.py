#!/usr/bin/env python3
"""把 Unity 場景裡【手調過】的門外佈局抽回 tools/west_layout.json。

為什麼需要它：Tools/Build Room 會 DestroyImmediate 整個 Room 重建，在 Editor 裡搬過的
位置下一次就沒了。腳本才是唯一真相、場景是產物——所以調完要抽回來。

手工回寫踩過的雷：改 RoomBuilder.cs 的碰撞圖時多打了一列，West 變 18 列，而
Collision = West.Zip(OfficeRows) 會【靜靜地】截到 17 列，整個門外往下位移一格。
所以這支只動 json、不動原始碼，並把「哪幾列的擋路格要跟著改」算好印出來讓人自己改。

用法：
  1. Unity 裡搬好家具 → Ctrl+S 存檔
  2. python3 tools/freeze_layout.py          # 看差異，不寫入
  3. python3 tools/freeze_layout.py --write  # 確認後寫入
  4. python3 tools/compose_room.py           # 重產素材
  5. Unity: Tools/Build Room 驗證重建結果與眼前一致
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENE = f"{ROOT}/unity/Assets/Scenes/SampleScene.unity"
LAYOUT = f"{ROOT}/tools/west_layout.json"
WEST_JSON = f"{ROOT}/limezu/_extracted/West/west.json"
ROOM_CS = f"{ROOT}/unity/Assets/Editor/RoomBuilder.cs"
CELL = 16


def scene_positions():
    """場景裡每個 GameObject 的 localPosition（世界單位）。"""
    txt = open(SCENE, encoding="utf-8", errors="ignore").read()
    names = {}
    for blk in txt.split("--- !u!1 &")[1:]:
        m = re.search(r"m_Name: (\S+)", blk)
        if m:
            names[blk.split("\n")[0].strip()] = m.group(1)
    pos = {}
    for blk in txt.split("--- !u!4 &")[1:]:
        g = re.search(r"m_GameObject: \{fileID: (\d+)\}", blk)
        p = re.search(r"m_LocalPosition: \{x: ([-\d.]+), y: ([-\d.]+)", blk)
        if g and p and g.group(1) in names:
            pos[names[g.group(1)]] = (float(p.group(1)), float(p.group(2)))
    return pos


def west_rows():
    src = open(ROOM_CS, encoding="utf-8").read()
    body = re.search(r"static readonly string\[\] West\s*=\s*\{(.*?)\n    \};", src, re.S)
    return re.findall(r'"([#.DT]+)"', body.group(1))


def main():
    write = "--write" in sys.argv
    pos = scene_positions()
    sizes = {i["name"]: (i["w"], i["h"]) for i in json.load(open(WEST_JSON))["items"]}
    layout = json.load(open(LAYOUT))
    west = west_rows()

    changed, missing, new = {}, [], dict(layout)
    for name in [k for k in layout if not k.startswith("_")]:
        if name not in pos:
            missing.append(name)
            continue
        if name not in sizes:
            continue
        wx, wy = pos[name]
        # pivot 左下 → 左上像素。四捨五入到整數：非整數像素在像素美術上會晃。
        px, py = round(wx * CELL), round(-wy * CELL) - sizes[name][1]
        if [px, py] != layout[name]:
            changed[name] = (layout[name], [px, py])
            new[name] = [px, py]

    if missing:
        print(f"⚠ 場景裡找不到：{', '.join(missing)}（還沒跑過 Build Room？）")
    if not changed:
        print("佈局與 west_layout.json 一致，沒有要凍結的東西。")
        return

    print(f"搬動了 {len(changed)} 件：")
    for n, (old, cur) in changed.items():
        print(f"  {n:<14} {tuple(old)} → {tuple(cur)}"
              f"　格({cur[0]/CELL:.1f},{(cur[1]+sizes[n][1])/CELL:.1f})")

    # 擋路格要跟著搬。這支不改原始碼——把該長什麼樣算出來給人改，錯了也看得見。
    want = {}
    for n, xy in new.items():
        if n.startswith("_") or n not in sizes:
            continue
        w, h = sizes[n]
        cy = (xy[1] + h - 1) // CELL
        for cx in range(xy[0] // CELL, (xy[0] + w - 1) // CELL + 1):
            if min(xy[0] + w, (cx + 1) * CELL) - max(xy[0], cx * CELL) > CELL * 0.45:
                want.setdefault(cy, set()).add(cx)

    print("\n門外各列該有的擋路格（RoomBuilder.cs 的 West，只列跟現況不同的）：")
    diff = 0
    for cy in sorted(want):
        row = west[cy]
        cur = {x for x, c in enumerate(row) if c == "T"}
        if cur == want[cy]:
            continue
        diff += 1
        new_row = "".join("T" if x in want[cy] else ("." if row[x] == "T" else row[x])
                          for x in range(len(row)))
        print(f'  第 {cy:>2} 列  "{row}" → "{new_row}"')
    if not diff:
        print("  （沒有要改的）")

    if write:
        json.dump(new, open(LAYOUT, "w"), ensure_ascii=False, indent=1)
        print(f"\n✓ 已寫入 {LAYOUT}。接著跑 tools/compose_room.py，再回 Unity 按 Build Room。")
    else:
        print("\n（唯讀模式。確認無誤後加 --write）")


if __name__ == "__main__":
    main()

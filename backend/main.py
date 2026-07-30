"""多 Agent 大腦後端（架構指南第 3 節落地）。

跑法：
  .venv/bin/uvicorn main:app --port 8123     # 在 backend/ 目錄下
  API key 放 backend/.env（ANTHROPIC_API_KEY=...；沒有的話 agent 隨機走動）

流程：
  Unity 連上 /ws → 送握手 {"type":"waypoints","agents":[...],"list":[...]}
  → 每個 agent 一個決策迴圈：decide()（Claude）→ 推指令給 Unity → arrived 寫回記憶
  → say 全員廣播進記憶（小辦公室聽得到）；say 帶 to＝搭話 → 展開回合對話
    （最多 MAX_ROUNDS 輪，對話中雙方主迴圈掛起）
  ponytail: 廣播不分距離——一間房 3 個人合理；房間多了再做鄰近過濾

手動介入照舊：
  curl -X POST localhost:8123/cmd -d '{"agent_id":"p17","action":"move_to","target":"cooler_1"}'
"""
import asyncio
import json
import os
import random
import re
import time
from collections import deque
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent import Agent, build_tools

load_dotenv()  # 讀 backend/.env（ANTHROPIC_API_KEY=...），已 gitignore

app = FastAPI()
# 多觀眾：桌面 Unity 與任意數量的 WebGL 分頁可同時連線，指令廣播給全部。
# （單一連線槽會讓分頁互相頂掉——新分頁搶走連線、關掉任一個就整條畫面鏈路歸零。）
viewers: set[WebSocket] = set()
events: list[dict] = []
agents: dict[str, Agent] = {}
arrived: dict[str, asyncio.Event] = {}
loops: list[asyncio.Task] = []
waypoint_list: list[str] = []
occupied: dict[str, str] = {}   # agent_id -> 佔用的 waypoint（防兩人擠同一點）
busy: set[str] = set()          # 對話/工作中的 agent（主迴圈掛起）
watchdog: asyncio.Task | None = None  # 工作失聯巡查（start_agents 時啟動）
canvas_stale = False  # 送了指令卻沒人回報：WebGL 分頁被瀏覽器凍結（WS 還開著，畫面已死）

client = anthropic.AsyncAnthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
if client is None:
    print("⚠ 未設定 ANTHROPIC_API_KEY——agent 將隨機走動（合約測試模式）")

# 純投影模式（OFFICE_MODE=projection）：生活大腦（Claude 決策/對話）停用，NPC 平時
# 零成本 idle（偶爾隨機走動），只有 /office/event 的真工作事件驅動行為。
# 生活模擬代碼保留不刪——平台方向完全確定後再清場（概念筆記 §九 決策方向反轉）。
PROJECTION = os.environ.get("OFFICE_MODE") == "projection"

DECISION_INTERVAL = (8.0, 25.0)  # 決策間隔秒數範圍（拉長省成本、縮短加快節奏）
IDLE_INTERVAL = (45.0, 120.0)    # 純投影模式的 idle 走動間隔（點綴用，別太熱鬧）
MAX_ROUNDS = 5                   # 對話回合上限（指南 §3：4~6 輪強制結束）
BUBBLE_WAIT = 2.8                # 每句話的展示間隔（等泡泡讀完）
BUBBLE_GAP = 2.2                 # 投影泡泡最小展示間隔（工具連發時後浪蓋前浪）
WORK_TIMEOUT = 300.0             # 上工中這麼久沒事件＝claw-cli 掛了（done 是 fire-and-forget 會丟）
WATCH_TICK = 30.0                # watchdog 巡查間隔


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    viewers.add(ws)
    print(f"✓ Unity 已連線（畫面 {len(viewers)} 個）")
    notify("roster")
    try:
        while True:
            evt = json.loads(await ws.receive_text())
            events.append(evt)
            await handle_event(evt)
    except WebSocketDisconnect:
        viewers.discard(ws)
        print(f"✗ Unity 斷線（剩 {len(viewers)} 個畫面）")
        if not viewers:  # 最後一個畫面走了才收生活迴圈
            stop_agents()
        notify("roster")


async def handle_event(evt: dict) -> None:
    global canvas_stale
    if canvas_stale:  # 有回音＝畫面活著
        canvas_stale = False
        notify("roster")
    kind = evt.get("type")
    if kind == "waypoints":
        start_agents(evt.get("agents", []), evt.get("list", []))
    elif kind == "arrived":
        aid = evt.get("agent_id", "")
        if aid in agents:
            agents[aid].location = evt.get("at", "?")
            agents[aid].remember(f"到了{evt.get('at')}")
            arrived[aid].set()
    else:
        print("事件:", evt)


def load_roster() -> None:
    """名冊啟動即載（脫鉤 Unity）：Web 外殼/Slack 派工不需要 Unity 在線；Unity 只是渲染面。"""
    persona_dir = Path(__file__).parent / "personas"
    for f in sorted(persona_dir.glob("p*.yaml")):
        agents[f.stem] = Agent(f.stem, persona_dir)
        arrived[f.stem] = asyncio.Event()
    print(f"名冊載入 {len(agents)} 位員工：{'、'.join(a.name for a in agents.values())}")


def start_agents(agent_ids: list[str], waypoints: list[str]) -> None:
    """Unity 握手：記 waypoint、開生活迴圈。名冊以 personas 為準（agent_ids 僅供對帳）。
    第二個畫面連進來時（同一組 waypoint）不重啟迴圈——多分頁只是多觀眾，世界只有一個。"""
    global waypoint_list
    if loops and waypoints == waypoint_list:
        print(f"（第 {len(viewers)} 個畫面加入，沿用進行中的世界）")
        return
    stop_agents()
    waypoint_list = waypoints
    for aid, a in agents.items():
        colleagues = [o.name for oid, o in agents.items() if oid != aid]
        tools = build_tools(waypoints, colleagues)
        loops.append(asyncio.create_task(agent_loop(a, tools)))
    global watchdog
    if watchdog is None or watchdog.done():
        watchdog = asyncio.create_task(work_watchdog())
    if PROJECTION:
        mode = "純投影（生活大腦停用，idle 走動）"
    else:
        mode = "Claude 決策" if client else "隨機走動（無 API key）"
    print(f"啟動 {len(agents)} 個 agent（{mode}），{len(waypoints)} 個互動點")


def stop_agents() -> None:
    """Unity 斷線：收渲染面的東西；工作狀態（busy/work_last/委派/黏性指派）比連線長壽。"""
    for t in loops:
        t.cancel()
    for t in pacers.values():
        t.cancel()
    loops.clear()
    occupied.clear()
    bubble_q.clear()
    pacers.clear()
    keep = set(work_last) | set(sub_active.values())
    busy.intersection_update(keep)  # 只清生活對話的 busy，工作中的不動
    notify("roster")


def find_by_name(name: str | None) -> Agent | None:
    if not name:
        return None
    for a in agents.values():
        if a.name == name:
            return a
    return None


def others_desc(me: Agent) -> str:
    return "，".join(
        f"{o.name}在{o.location}" for oid, o in agents.items() if oid != me.id
    )


def broadcast_say(speaker: Agent, text: str, target: Agent | None) -> None:
    """一間房大家都聽得到：對象記「對我說」，旁人記「聽到」。"""
    for oid, other in agents.items():
        if oid == speaker.id:
            continue
        if target is not None and oid == target.id:
            other.remember(f"{speaker.name}對我說「{text}」")
        else:
            other.remember(f"聽到{speaker.name}說「{text}」")


async def send_cmd(cmd: dict) -> bool:
    """廣播給所有畫面；送不出去的當場移除（半開連線不會拖住其他觀眾）。"""
    if not viewers:
        return False
    payload = json.dumps(cmd, ensure_ascii=False)
    dead = []
    for ws in list(viewers):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        viewers.discard(ws)
    return bool(viewers)


async def converse(a: Agent, b: Agent, opening: str) -> None:
    """對話回合迴圈（指南 §3）：a 對 b 說了 opening，之後輪流回話，上限 MAX_ROUNDS。"""
    busy.add(a.id)
    busy.add(b.id)
    print(f"💬 {a.name} ↔ {b.name} 開聊：「{opening}」")
    try:
        speaker, listener, line = b, a, opening
        for _ in range(MAX_ROUNDS):
            await asyncio.sleep(BUBBLE_WAIT)  # 等上一句泡泡讀完
            try:
                act = await speaker.decide_reply(client, listener.name, line)
            except Exception as e:
                print(f"⚠ {speaker.id} 回話失敗（{type(e).__name__}），散會")
                break
            if not act or act.get("action") != "reply" or not act.get("text"):
                print(f"💬 {speaker.name} 結束了對話")
                break
            line = act["text"]
            if not await send_cmd({"agent_id": speaker.id, "action": "say",
                                   "channel": "public", "text": line}):
                break
            print(f"💬 {speaker.name} → {listener.name}：「{line}」")
            speaker.remember(f"回{listener.name}「{line}」")
            broadcast_say(speaker, line, listener)
            speaker, listener = listener, speaker
        else:
            print(f"💬 {a.name} ↔ {b.name} 聊到回合上限，散會")
    finally:
        busy.discard(a.id)
        busy.discard(b.id)


async def agent_loop(a: Agent, tools: list[dict]) -> None:
    await asyncio.sleep(random.uniform(1.0, 6.0))  # 錯開起步
    while viewers:
        while a.id in busy:  # 對話中掛起主迴圈
            await asyncio.sleep(1.0)

        try:
            if client is not None and not PROJECTION:
                actions = await a.decide(client, tools, others_desc(a))
            else:  # 零成本 idle：不打 API，偶爾走動點綴
                actions = [{"action": "move_to", "target": random.choice(waypoint_list)}]
        except Exception as e:
            print(f"⚠ {a.id} decide 失敗（{type(e).__name__}: {e}），隨機走")
            actions = [{"action": "move_to", "target": random.choice(waypoint_list)}]

        for act in actions:
            if not viewers:
                return
            if a.id in busy:  # 剛被人搭話，放棄剩餘動作
                break

            if act["action"] == "move_to":
                # 佔位守衛：目的地被同事佔用就換空位（大腦知道同事動態，這是硬保險）
                taken = {t for aid, t in occupied.items() if aid != a.id}
                target = act.get("target")
                if not target:
                    continue
                if target in taken:
                    free = [w for w in waypoint_list if w not in taken]
                    if not free:
                        continue
                    new_target = random.choice(free)
                    a.remember(f"想去{target}但有人，改去{new_target}")
                    target = new_target
                    act = {"action": "move_to", "target": target}
                occupied[a.id] = target

                await send_cmd({"agent_id": a.id, **act})
                print(f"{a.name}: {act}")
                a.remember(f"出發去{target}")
                arrived[a.id].clear()
                try:
                    await asyncio.wait_for(arrived[a.id].wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    a.remember("剛剛走去某處超時沒走到")
                    global canvas_stale
                    if not canvas_stale:  # 指令沒回音：畫面十之八九凍結了，讓外殼顯示出來
                        canvas_stale = True
                        print("⚠ 移動指令無回應——WebGL 分頁可能被凍結（重整該分頁）")
                        notify("roster")

            elif act["action"] == "say":
                text = act.get("text", "")
                if not text:
                    continue
                await send_cmd({"agent_id": a.id, "action": "say",
                                "channel": "public", "text": text})
                print(f"{a.name}: {act}")
                a.remember(f"說了「{text}」")
                target_agent = find_by_name(act.get("to"))
                broadcast_say(a, text, target_agent)
                # 搭話且對方有空 → 展開對話迴圈
                if target_agent and target_agent.id not in busy and client is not None:
                    await converse(a, target_agent, text)

        await asyncio.sleep(random.uniform(*(IDLE_INTERVAL if PROJECTION else DECISION_INTERVAL)))


# ── Office 投影：cogito-agent 真工作事件 → 像素辦公室狀態（概念筆記 §十）────────
# 契約（cogito 側 OfficeReporter 同步維護）：
#   POST /office/event {"agent":"p17","kind":"...","label":"...","detail":"..."}
#   kind ∈ start/turn/think/tool/result/error/msg/done
# 投影表：start→走工位坐下+泡任務、tool→泡「▸工具」、error→泡「✗工具」、
#   msg→泡內容、done→泡收工+釋放；think/turn/result 不投影（太吵）。
# 真工作中 agent 進 busy（生活模擬掛起）——工作永遠蓋過生活閒逛。
WORK_DESK = {"p17": "chair_1", "p01": "chair_2", "p07": "chair_3"}  # 上工的固定工位
BOSS_DOOR = "boss_1"  # 老闆房走道：等 HITL 審批時站這裡
SUB_RE = re.compile(r"^\[Subagent(?::([^\]]+))?\]\s*")  # cogito 子 agent 事件前綴


def say(aid: str, text: str) -> dict:
    return {"agent_id": aid, "action": "say", "channel": "public", "text": text}


async def goto(aid: str, target: str) -> None:
    """走位＋佔位登記（工作投影專用；生活迴圈有自己的佔位守衛）。"""
    occupied[aid] = target
    await send_cmd({"agent_id": aid, "action": "move_to", "target": target})


# ── 投影泡泡節流：每個 NPC 一條佇列 + 播報器，最小間隔 BUBBLE_GAP。
# 工具泡（collapsible）連發時後浪蓋前浪只保最新；重要泡（任務/回報/收工）必播。
bubble_q: dict[str, list[tuple[str, bool]]] = {}   # aid -> [(text, collapsible)]
pacers: dict[str, asyncio.Task] = {}


async def bubble(aid: str, text: str, collapsible: bool = False) -> None:
    q = bubble_q.setdefault(aid, [])
    if collapsible and q and q[-1][1]:
        q[-1] = (text, True)
    else:
        q.append((text, collapsible))
    if aid not in pacers or pacers[aid].done():
        pacers[aid] = asyncio.create_task(drain_bubbles(aid))


async def drain_bubbles(aid: str) -> None:
    q = bubble_q.get(aid, [])
    while q:
        text, _ = q.pop(0)
        await send_cmd(say(aid, text))
        await asyncio.sleep(BUBBLE_GAP)


# ── 失聯保險：done 是 fire-and-forget，claw-cli 被砍/崩潰時不會送達，
# busy 就永遠不釋放。橋端記最後事件時刻，逾時自動釋放（含名下委派卡）。
work_last: dict[str, float] = {}  # 上工中的 agent -> 最後事件時刻


def release_work(aid: str) -> None:
    """收工/失聯：釋放主 agent 與其名下所有委派卡（沒回報的委派標 lost，不留永久 working）。"""
    work_last.pop(aid, None)
    busy.discard(aid)
    for key in [k for k in sub_active if k[0] == aid]:
        npc = sub_active.pop(key)
        busy.discard(npc)
        close_card(npc, "lost")
    notify("roster")


async def sweep_work() -> None:
    now = time.monotonic()
    for aid in [a for a, t in list(work_last.items()) if now - t > WORK_TIMEOUT]:
        print(f"⚠ {aid} 上工中 {WORK_TIMEOUT:.0f}s 無事件，視為失聯，釋放")
        release_work(aid)
        close_card(aid, "lost")
        log_ev(aid, "⚠ 任務失聯中斷")
        if aid in agents:
            agents[aid].remember("工作任務失聯中斷了")
            await bubble(aid, "⚠ 失聯沒回應")


async def work_watchdog() -> None:
    while True:
        await asyncio.sleep(WATCH_TICK)
        await sweep_work()


# 泡泡＝狀態短語，不是內文（泡泡只有 8 字寬，內文看右側工作串）。
# 用字必須在 OfficeFontImporter.Chars 的字表內——那份圖集字型只烘了這些字，
# 沒烘到的字在 WebGL 會是空白（內建 Arial 無中文字形，編輯器才靠系統字型補字）。
BUBBLE = {"start": "▶ 接到任務", "tool": "● 執行中…", "error": "⚠ 出錯了", "msg": "→ 回報"}


def office_bubble(kind: str, label: str) -> str | None:
    """事件 → NPC 頭上的狀態泡泡；None＝這種事件不冒泡。"""
    if kind == "done":
        return "✓ 完成" if label == "ok" else "⚠ 中斷"
    return BUBBLE.get(kind)


# 子 agent 投影（§十-1 第二刀）：spawn 具名 agent 映射到閒置 NPC 真走位——
# 阿哲委派 code-reviewer，小美就起身入座開工；內部事件泡泡掛到她頭上，收工冒回報泡。
# 沒人有空時退回舊行為（主 agent 頭上帶小名冒泡）。cogito / Unity 零改動。
SUB_NPC = {"code-reviewer": "p01", "planner": "p01", "security-auditor": "p07"}  # 慣用人選
SPAWN_RE = re.compile(r"^spawn_subagent(?::(\S+))?")
sub_active: dict[tuple[str, str], str] = {}  # (主 agent, 子 agent 名) -> 演出的 NPC


def pick_sub_npc(parent: str, name: str) -> str | None:
    free = [x for x in agents if x != parent and x not in busy]
    cand = SUB_NPC.get(name)
    if cand in free:
        return cand
    return free[0] if free else None


async def project_sub(parent: str, kind: str, label: str, detail: str) -> bool:
    """子 agent 事件分流；回傳 True＝已投影完畢（主流程不再處理）。"""
    spawn = SPAWN_RE.match(label)
    if spawn:
        name = spawn.group(1) or ""  # 無名子 agent（探路者）兩側都正規化成 ""
        shown = name or "探路者"
        if kind == "tool":  # 委派上工
            npc = pick_sub_npc(parent, name)
            if npc is None:
                return False  # 沒人有空：主 agent 頭上冒泡就好
            sub_active[(parent, name)] = npc
            busy.add(npc)
            notify("roster")
            child = report_card(npc, f"支援{agents[parent].name}：{shown}")
            if desk := WORK_DESK.get(npc):
                await goto(npc, desk)
            await bubble(parent, "→ 交辦")
            await bubble(npc, "★ 支援中")
            # 委派事件掛上子卡引用 → 主任務串裡直接看得到對方在做什麼
            log_ev(parent, f"🤝 委派給 {agents[npc].name}：{shown}",
                   sub={"agent": npc, "id": child["id"]})
            log_ev(npc, f"📋 支援{agents[parent].name}：{shown}")
            agents[npc].remember(f"接下{agents[parent].name}委派的{shown}工作")
            return True
        if kind in ("result", "error"):  # 委派收工
            npc = sub_active.pop((parent, name), None)
            if npc is None:
                return False
            busy.discard(npc)
            notify("roster")
            close_card(npc, "ok" if kind == "result" else "error")
            card = last_report.get(npc)
            if card:
                card["report"] = detail
            mark = "✔ 回報：" if kind == "result" else "✗ 失敗："
            await bubble(npc, "✓ 回報完成" if kind == "result" else "⚠ 回報出錯了")
            log_ev(npc, f"{mark}{detail[:200]}")
            agents[npc].remember("完成了委派工作" if kind == "result" else "委派的工作失敗了")
            return True
        return True  # spawn 的其他事件不投影
    sub = SUB_RE.match(label)
    if sub:
        npc = sub_active.get((parent, sub.group(1) or ""))
        if npc is None:
            return False  # 沒開成卡：退回主 agent 帶小名冒泡
        stripped = SUB_RE.sub("", label)
        text = office_bubble(kind, stripped)
        if text:
            await bubble(npc, text, collapsible=kind == "tool")
        line = tl_text(kind, stripped, detail)
        if line:
            log_ev(npc, line)
        return True
    return False


# Slack/Telegram 常駐派工（cogito chatbot Core）：事件的 agent 是頻道 id（"slack:C123"）
# 而非 persona id。未知 id 動態指派一個閒置 NPC，黏性映射——同頻道固定同員工，橋重啟才重配。
conv_npc: dict[str, str] = {}  # 頻道 id -> persona id

# 工作紀錄按「任務卡」組織：每位 NPC 一串歷史卡（最多 20 張），每張卡自帶事件串。
# last_report 指向進行中/最新的那張。刻意不隨 Unity 斷線清掉——工作紀錄比連線長壽。
last_report: dict[str, dict] = {}
history: dict[str, deque] = {}   # aid -> deque[任務卡]
MAX_CARD_EVENTS = 150            # 單卡事件上限（工具連發截舊保新）


_card_seq = 0  # 任務卡編號：委派事件用它把子卡掛回主任務串（跨員工引用）


def report_card(aid: str, task: str, workdir: str = "") -> dict:
    global _card_seq
    _card_seq += 1
    card = {"id": _card_seq, "task": task, "status": "working", "report": "",
            "workdir": workdir,  # cogito 該會話的工作目錄——產出落在哪，看板直接標出來
            "events": [], "at": time.strftime("%H:%M"), "end": ""}
    history.setdefault(aid, deque(maxlen=20)).append(card)
    last_report[aid] = card
    return card


def close_card(aid: str, status: str) -> None:
    card = last_report.get(aid)
    if card and card["status"] == "working":
        card["status"] = status
        card["end"] = time.strftime("%H:%M")


# ── SSE 推播（失效通知模式）：只推「誰變了」，畫面收到再回抓既有 API——
# 單一資料權威（GET endpoints），不維護第二條全量資料路徑。
subscribers: set[asyncio.Queue] = set()
_dirty = False  # 有狀態變更待落地（存檔器每 2 秒巡）


def notify(kind: str, aid: str = "") -> None:
    global _dirty
    _dirty = True  # 所有狀態變更都會走到 notify——持久化搭同一班車
    for q in list(subscribers):
        if q.qsize() < 100:  # 塞爆代表客戶端死了，丟事件等它斷線清理
            q.put_nowait({"type": kind, "id": aid})


def log_ev(aid: str, text: str, sub: dict | None = None) -> None:
    """sub＝{agent, id}：這是一則委派事件，前端在此處內嵌對方的子任務卡。"""
    card = last_report.get(aid) or report_card(aid, "（雜項）")
    ev: dict = {"at": time.strftime("%H:%M:%S"), "text": text}
    if sub:
        ev["sub"] = sub
    card["events"].append(ev)
    if len(card["events"]) > MAX_CARD_EVENTS:
        del card["events"][0]
    notify("agent", aid)


def tl_text(kind: str, label: str, detail: str) -> str | None:
    """事件 → 時間軸一行；None＝不記（think/turn）。比泡泡完整（帶參數/結果預覽）。"""
    if kind == "tool":
        return f"▸ {label}" + (f"｜{detail[:80]}" if detail else "")
    if kind == "result":
        return f"✓ {label}"
    if kind == "error":
        return f"✗ {label}" + (f"：{detail[:120]}" if detail else "")
    if kind == "msg":
        return label[:200]
    return None


def resolve_npc(ext: str) -> str | None:
    if ext in agents:  # claw-cli 直接指名 persona，原路
        return ext
    if ext.startswith("office:") and ext.split(":", 1)[1] in agents:
        return ext.split(":", 1)[1]  # Web 外殼派工：conv 就是指名的員工
    if ext in conv_npc:
        return conv_npc[ext]
    free = [x for x in agents if x not in busy and x not in conv_npc.values()]
    if not free:
        return None  # 全員有主：事件丟棄（辦公室演不了，任務本身照跑）
    conv_npc[ext] = free[0]
    print(f"🪪 頻道 {ext} 指派給 {agents[free[0]].name}（{free[0]}）")
    return free[0]


@app.post("/office/event")
async def office_event(ev: dict):
    kind, label = ev.get("kind", ""), ev.get("label", "")
    aid = resolve_npc(ev.get("agent", ""))
    if aid is None:  # Unity 不在線也照收：時間軸/報告卡是資料面，投影指令會自動 no-op
        return {"ok": False, "error": "沒有可指派的 NPC"}
    a = agents[aid]

    if aid in work_last or kind == "start":  # 上工中任何事件（含 think/turn）都算心跳
        work_last[aid] = time.monotonic()

    if await project_sub(aid, kind, label, ev.get("detail", "")):
        return {"ok": True}

    # 審批逾時（自動拒絕）後工作恢復：人還杵在老闆房門口，看到工具事件就自己回工位
    if kind == "tool" and aid not in pending_approval and occupied.get(aid) == BOSS_DOOR:
        if desk := WORK_DESK.get(aid):
            await goto(aid, desk)

    if kind == "start":  # 上工：掛起生活模擬、走到工位（自動入座），任務泡
        busy.add(aid)
        notify("roster")
        report_card(aid, label, ev.get("detail", ""))  # start 的 detail＝工作目錄
        log_ev(aid, f"📋 接到任務：{label}")
        if desk := WORK_DESK.get(aid):
            await goto(aid, desk)
        a.remember(f"接到工作任務「{label}」，開始上工")
    elif kind == "msg":  # 報告全文進卡（泡泡另外截短）
        card = last_report.get(aid) or report_card(aid, "（橋重啟，任務開頭沒記到）")
        card["report"] = label
        log_ev(aid, label[:200])
    elif kind == "done":  # 收工：釋放主 agent＋名下委派卡，回歸 idle
        close_card(aid, "ok" if label == "ok" else "error")
        card = last_report.get(aid)
        if card and label != "ok" and ev.get("detail"):
            card["report"] = card["report"] or ev["detail"]
        pending_approval.pop(aid, None)  # 任務結束，殘留審批卡（逾時自動拒絕）一併收掉
        release_work(aid)
        log_ev(aid, "✔ 任務完成" if label == "ok" else "✗ 任務中斷")
        a.remember("完成了手上的工作任務" if label == "ok" else "工作任務中斷了")
    else:
        # 一般事件進時間軸（fallback 的 [Subagent:名] 前綴轉小名，跟泡泡一致）
        lbl = label
        m = SUB_RE.match(lbl)
        if m:
            lbl = f"{m.group(1) or '手下'}·{SUB_RE.sub('', lbl)}"
        line = tl_text(kind, lbl, ev.get("detail", ""))
        if line:
            log_ev(aid, line)

    text = office_bubble(kind, label)
    if text:
        await bubble(aid, text, collapsible=kind == "tool")
    return {"ok": True}


@app.get("/office/report/{aid}")
def office_report(aid: str):
    """Unity/Web 點員工查看最近一次任務的報告卡。status: working/ok/error/lost。"""
    card = last_report.get(aid)
    if not card:
        return {"ok": False, "error": "這位員工還沒有工作紀錄"}
    name = agents[aid].name if aid in agents else aid
    return {"ok": True, "agent": aid, "name": name, **card,
            "approval": pending_approval.get(aid, ""),
            "timeline": card["events"],  # Unity ReportViewer 相容：最新卡的事件串
            "history": [with_subcards(t) for t in history.get(aid, [])]}


def find_card(agent: str, cid: int) -> dict | None:
    return next((t for t in history.get(agent, []) if t.get("id") == cid), None)


def with_subcards(card: dict) -> dict:
    """把委派事件的子卡展開進主任務串——一條串就看得到「誰把工作分給誰、對方做了什麼」。"""
    out = dict(card)
    out["events"] = []
    for e in card["events"]:
        ev = dict(e)
        ref = ev.pop("sub", None)
        child = find_card(ref["agent"], ref["id"]) if ref else None
        if child:
            ev["subcard"] = {**child, "agent": ref["agent"],
                             "name": agents[ref["agent"]].name if ref["agent"] in agents else ref["agent"]}
        out["events"].append(ev)
    return out


# ── Web 派工與回訊（cogito 的 office 平台，cmd/claw 設 COGITO_HTTP_ADDR/TOKEN 開啟）────
COGITO_HTTP = os.environ.get("COGITO_HTTP", "")          # cogito HTTP 入口，如 http://localhost:8787
COGITO_HTTP_TOKEN = os.environ.get("COGITO_HTTP_TOKEN", "")
APPROVAL_PREFIX = "⚠️ *高危操作審批請求*"                  # chatbot approval.go 的卡片開頭
PROGRESS_PREFIXES = ("🤔", "🛠️", "✅ *執行成功*", "⚠️ *執行報錯*")  # OfficeReporter 已投影過，去重
pending_approval: dict[str, str] = {}  # npc id -> 待審批卡文字（shell 顯示核准/駁回按鈕）


@app.post("/office/chat")
async def office_chat(ev: dict):
    """cogito office 平台的出訊（審批卡/完成/失敗訊息）→ 時間軸；進度類已由事件流投影，濾掉。"""
    text = (ev.get("text") or "").strip()
    aid = resolve_npc(ev.get("agent", ""))
    if aid is None or not text:
        return {"ok": False}
    if text.startswith(PROGRESS_PREFIXES):
        return {"ok": True}
    if text.startswith(APPROVAL_PREFIX):
        pending_approval[aid] = text
        # 等審批也算工作中：不標 busy 的話生活 idle 迴圈會把罰站走位蓋掉
        busy.add(aid)
        work_last[aid] = time.monotonic()
        notify("roster")
        # 走到老闆房門口站著等（門口被別人佔著就原地等，不擠）
        if BOSS_DOOR not in {t for a, t in occupied.items() if a != aid}:
            await goto(aid, BOSS_DOOR)
        await bubble(aid, "⚠ 等待審批")
    log_ev(aid, f"💬 {text[:300]}")
    return {"ok": True}


@app.get("/office/status")
def office_status():
    """畫面/派工鏈路健康度——WebGL 分頁被瀏覽器凍結時 WS 仍開著，指令會送進黑洞，
    這個旗標讓外殼直接顯示「畫面未連線」而不是讓人猜為什麼 NPC 不動。"""
    return {"canvas": len(viewers), "stale": canvas_stale,
            "live": bool(loops), "dispatch": bool(COGITO_HTTP)}


@app.get("/office/stream")
async def office_stream():
    """SSE：狀態失效通知（roster / agent）。畫面收到再回抓 /agents、/office/report。"""
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)

    async def gen():
        try:
            yield 'data: {"type":"hello"}\n\n'
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/office/dispatch")
async def office_dispatch(d: dict):
    """Web 外殼派工/審批 → 轉發 cogito HTTP 入口（token 在橋端，瀏覽器拿不到）。"""
    aid, text = d.get("agent", ""), (d.get("text") or "").strip()
    if aid not in agents or not text:
        return {"ok": False, "error": "缺 agent 或 text"}
    # 防呆：工作中不收新任務（cogito 也會擋，這裡先給即時回饋）；approve/reject 是任務中互動，放行
    if aid in busy and text.split()[0] not in ("approve", "reject"):
        return {"ok": False, "error": f"{agents[aid].name} 正在工作中，收工後再派新任務"}
    if not COGITO_HTTP:
        return {"ok": False, "error": "未設 COGITO_HTTP——cogito 的 HTTP 派工入口未啟用"}
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            r = await cl.post(f"{COGITO_HTTP}/task", json={"agent": aid, "text": text},
                              headers={"Authorization": f"Bearer {COGITO_HTTP_TOKEN}"})
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"cogito 入口連不上：{type(e).__name__}"}
    if r.status_code != 202:
        return {"ok": False, "error": f"cogito 回 {r.status_code}：{r.text[:120]}"}
    verb = text.split()[0]
    if verb in ("approve", "reject"):
        pending_approval.pop(aid, None)  # cogito 確認收到才收卡
        notify("agent", aid)
        await bubble(aid, "✓ 放行" if verb == "approve" else "⚠ 駁回")
        log_ev(aid, f"🧑‍💼 老闆{'核准' if verb == 'approve' else '駁回'}了這個操作")
        if desk := WORK_DESK.get(aid):  # 審批完回工位繼續
            await goto(aid, desk)
    else:
        log_ev(aid, f"🧑‍💼 老闆交辦：{text[:200]}")
    return {"ok": True}


@app.post("/cmd")
async def cmd(c: dict):
    if not await send_cmd(c):
        return {"ok": False, "error": "Unity 未連線"}
    return {"ok": True, "sent": c}


@app.get("/events")
def get_events():
    return events[-20:]


@app.get("/agents")
def get_agents():
    return {
        aid: {"name": a.name, "role": a.persona.get("role", "員工"),
              "team": a.persona.get("team", "未分組"),
              "location": a.location, "busy": aid in busy,
              "memory": list(a.memory)}
        for aid, a in agents.items()
    }


# ── 持久化：任務卡/黏性指派/審批卡落地 JSON——橋重啟不失憶（cogito 的 session 本來就落地，
# 這邊補齊對稱）。notify() 兼作 dirty 標記，存檔器每 2 秒批次寫（原子寫入：tmp + rename）。
STATE_FILE = Path(os.environ.get("OFFICE_STATE") or Path(__file__).parent / "office_state.json")


def save_state() -> None:
    global _dirty
    _dirty = False
    data = {"history": {a: list(cards) for a, cards in history.items()},
            "conv_npc": conv_npc, "pending_approval": pending_approval}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def load_state() -> None:
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"⚠ 工作紀錄載入失敗（忽略舊檔）：{e}")
        return
    global _card_seq
    for aid, cards in data.get("history", {}).items():
        history[aid] = deque(cards, maxlen=20)
        if cards:
            last_report[aid] = history[aid][-1]  # 重建「最新卡」指標
            _card_seq = max([_card_seq] + [t.get("id", 0) for t in cards])  # 續號，別撞到舊卡
    conv_npc.update(data.get("conv_npc", {}))
    pending_approval.update(data.get("pending_approval", {}))
    for aid, card in last_report.items():
        if card["status"] == "working":  # 重啟時任務可能還在跑：先當在跑，事件續流；死了 watchdog 兜底
            busy.add(aid)
            work_last[aid] = time.monotonic()
    n = sum(len(c) for c in history.values())
    if n:
        print(f"工作紀錄載入：{len(history)} 位員工、{n} 張任務卡")


async def state_saver() -> None:
    while True:
        await asyncio.sleep(2.0)
        if _dirty:
            try:
                save_state()
            except OSError as e:
                print(f"⚠ 工作紀錄存檔失敗：{e}")


@app.on_event("startup")
async def _startup() -> None:
    load_state()
    # watchdog 脫鉤 Unity：純 Web 派工（不開 Unity）失聯保險也要在
    global watchdog
    if watchdog is None or watchdog.done():
        watchdog = asyncio.create_task(work_watchdog())
    asyncio.create_task(state_saver())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _dirty:
        save_state()  # Ctrl+C 前最後一趟


load_roster()  # 名冊啟動即載：Web 外殼/派工不等 Unity


# ── Web 外殼（§十-5 里程碑）：/shell 是 Pixffice 式佈局頁；/unity 伺服 WebGL build
# （Unity 選單 Tools → Build WebGL 產出）。同源伺服＝零 CORS 設定。
_ROOT = Path(__file__).parent.parent
_WEBGL = _ROOT / "unity" / "Builds" / "WebGL"
if _WEBGL.exists():
    app.mount("/unity", StaticFiles(directory=_WEBGL, html=True), name="unity")
else:
    print("ℹ 尚無 WebGL build（unity/Builds/WebGL）——/shell 中間畫布會提示先去 Unity 建置")
app.mount("/shell", StaticFiles(directory=_ROOT / "web", html=True), name="shell")
# 名冊頭像（LimeZu 衍生物，不進 git）：跑 tools/make_avatars.py 產生；沒有就用文字頭像頂替
_AVATARS = Path(__file__).parent / "avatars"
if _AVATARS.exists():
    app.mount("/avatars", StaticFiles(directory=_AVATARS), name="avatars")
else:
    print("ℹ 尚無名冊頭像——跑 python3 tools/make_avatars.py 產生（外殼先用文字頭像）")

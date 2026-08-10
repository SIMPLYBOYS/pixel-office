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
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent import Agent, build_tools

load_dotenv()  # 讀 backend/.env（ANTHROPIC_API_KEY=...），已 gitignore

app = FastAPI()
# 多觀眾：桌面 Unity 與任意數量的 WebGL 分頁可同時連線，指令廣播給全部。
# （單一連線槽會讓分頁互相頂掉——新分頁搶走連線、關掉任一個就整條畫面鏈路歸零。）
viewers: set[WebSocket] = set()
events: list[dict] = []
agents: dict[str, Agent] = {}

# ── 看板：一張沒有 NPC 的名冊卡。
# kanban 是「模式」不是「人」——同一個任務交給整個團隊，而不是某一個人。放進 agents 是為了讓
# 任務卡、工作串、歷史分組、檔案預覽【整套沿用】（那些全部只認 aid 字串）；代價是要在兩個地方
# 把它擋掉：生活迴圈（它沒有身體）與投影走位（它沒有工位）。擋在共用函式而不是每個呼叫點——
# 新增的投影動作漏擋就會露餡，擋在源頭才不用靠紀律。
# 對應的 cogito 會話是 office:kanban，記憶與 .claw/agents/ 都落在它自己的目錄（共同記憶）。
KANBAN = "kanban"


def npcs() -> dict[str, Agent]:
    """真的有身體的那些。生活迴圈、同事名單、頻道自動指派都只看這個。"""
    return {aid: a for aid, a in agents.items() if aid != KANBAN}
arrived: dict[str, asyncio.Event] = {}
loops: list[asyncio.Task] = []
waypoint_list: list[str] = []
occupied: dict[str, str] = {}   # agent_id -> 佔用的 waypoint（防兩人擠同一點）
busy: set[str] = set()          # 對話/工作中的 agent（主迴圈掛起）
watchdog: asyncio.Task | None = None  # 工作失聯巡查（start_agents 時啟動）
canvas_stale = False  # 送了指令卻沒人回報：WebGL 分頁被瀏覽器凍結（WS 還開著，畫面已死）
projection_offline = False  # 走位送不出去（沒有畫面在線）——只在狀態轉換時印，不刷屏
# 子 agent 被徵用的時間（npc -> monotonic）。釋放事件掉了就沒人放他，於是那個 NPC 永遠
# 卡在 busy、永遠不回座位——sweep_work 靠這張表兜底。主 agent 的失聯有 work_last 管，
# 但那條只在【主 agent 自己】沒事件時觸發：主 agent 還活著、只有子 agent 的釋放掉了的
# 情況，以前完全沒有人收拾。
sub_since: dict[str, float] = {}

client = anthropic.AsyncAnthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
if client is None:
    print("⚠ 未設定 ANTHROPIC_API_KEY——agent 將隨機走動（合約測試模式）")

# 純投影模式（OFFICE_MODE=projection）：生活大腦（Claude 決策/對話）停用，NPC 平時
# 零成本 idle（偶爾隨機走動），只有 /office/event 的真工作事件驅動行為。
# 生活模擬代碼保留不刪——平台方向完全確定後再清場（概念筆記 §九 決策方向反轉）。
PROJECTION = os.environ.get("OFFICE_MODE") == "projection"

DECISION_INTERVAL = (8.0, 25.0)  # 決策間隔秒數範圍（拉長省成本、縮短加快節奏）
IDLE_INTERVAL = (45.0, 120.0)    # 純投影模式的 idle 走動間隔（點綴用，別太熱鬧）
IDLE_SLEEP = 600.0               # 這麼久沒接到工作＝回工位趴著（「這位沒事做」要看得出來）
MAX_ROUNDS = 5                   # 對話回合上限（指南 §3：4~6 輪強制結束）
BUBBLE_WAIT = 2.8                # 每句話的展示間隔（等泡泡讀完）
BUBBLE_GAP = 2.2                 # 投影泡泡最小展示間隔（工具連發時後浪蓋前浪）
WORK_TIMEOUT = 300.0             # 上工中這麼久沒事件＝claw-cli 掛了（done 是 fire-and-forget 會丟）
STUCK_AFTER = 60.0               # 上工中這麼久【只有 think/turn】沒有任何工具事件＝卡住了，去接杯水
SUB_TIMEOUT = 420.0              # 子 agent 被徵用這麼久還沒收到釋放事件＝那則事件掉了，強制放人
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
        # 閒置計時從【現在】起算：預設 0.0 的話，「從沒接過任務」會被算成「閒了很久」，
        # 於是一開機全員直接回工位趴著，再也不會走動（實測到的）。
        last_work[f.stem] = time.monotonic()
    print(f"名冊載入 {len(agents)} 位員工：{'、'.join(a.name for a in agents.values())}")
    # 看板放在名冊【之後】才加：上面那行的人數才不會把它算成員工。
    agents[KANBAN] = Agent(KANBAN, persona_dir)


def start_agents(agent_ids: list[str], waypoints: list[str]) -> None:
    """Unity 握手：記 waypoint、開生活迴圈。名冊以 personas 為準（agent_ids 僅供對帳）。
    第二個畫面連進來時（同一組 waypoint）不重啟迴圈——多分頁只是多觀眾，世界只有一個。"""
    global waypoint_list
    if loops and waypoints == waypoint_list:
        print(f"（第 {len(viewers)} 個畫面加入，沿用進行中的世界）")
        return
    stop_agents()
    waypoint_list = waypoints
    for aid, a in npcs().items():   # 看板沒有身體，不進生活迴圈也不算同事
        colleagues = [o.name for oid, o in npcs().items() if oid != aid]
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
    # sub_active 的值是【list】（同名子 agent 可並行），set() 直接包會 TypeError:
    # unhashable type: 'list'——於是「有子 agent 在跑時觀眾重連」會讓整個握手炸掉，
    # 世界停在半死狀態。攤平才對。
    keep = set(work_last) | {npc for q in sub_active.values() for npc in q}
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
            elif a.id in sleeping:
                actions = []   # 已經趴著了：安靜等下一輪（有事件會把他叫醒）
            elif time.monotonic() - last_work.get(a.id, 0.0) > IDLE_SLEEP:
                # 太久沒接到工作：回自己工位趴著。這是狀態投影不是裝飾——
                # 「閒晃」與「沒事做」在畫面上長得一樣，久了看板就失去訊息量。
                desk = WORK_DESK.get(a.id)
                actions = [{"action": "move_to", "target": desk}, {"action": "use", "target": "sleep"}] \
                    if desk else [{"action": "use", "target": "sleep"}]
                sleeping.add(a.id)
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

            if act["action"] == "use":
                await pose(a.id, act["target"])
                continue

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
#   msg→泡內容、done→泡收工+釋放；think/result 不投影（太吵）。
#   turn→只寫【工作串】不冒泡：泡泡只有 8 字寬又有節流，多一則是噪音；但少了它，
#   兩次工具呼叫之間的長考在時間軸上是一段全空白，看起來像 agent 掛了。
# 真工作中 agent 進 busy（生活模擬掛起）——工作永遠蓋過生活閒逛。
WORK_DESK = {"p17": "chair_1", "p01": "chair_2", "p07": "chair_3",   # 上工的固定工位
             "p05": "chair_4", "p12": "chair_5",
             "p19": "boss_seat"}   # CTO 的位子在老闆房裡（persona 就寫他多半待在那），坐著辦公
BOSS_DOOR = "boss_1"  # 老闆房走道：等 HITL 審批時站這裡（面向老闆桌）
BOARD = "board_1"     # 白板前：規劃類子 agent 站這裡，不佔工位
COOLER = "cooler_1"   # 飲水機：卡住太久的人去接杯水（think 空轉的投影）
# 各工位【旁邊】的站位：委派時主 agent 走過去，面向坐著的同事——
# 「兩個人在同一張桌子旁」是唯一看得出「他們在協作」的畫面語言。
DESK_SIDE = {"p17": "side_1", "p01": "side_2", "p07": "side_3",
             "p05": "side_4", "p12": "side_5", "p19": BOSS_DOOR}
SUB_RE = re.compile(r"^\[Subagent(?::([^\]]+))?\]\s*")  # cogito 子 agent 事件前綴


def say(aid: str, text: str) -> dict:
    return {"agent_id": aid, "action": "say", "channel": "public", "text": text}


async def pose(aid: str, action: str) -> None:
    """讓 NPC 擺一個姿勢（move_to 會自動清掉，不必手動還原）。
    只投影「站著不動看不出來」的狀態——等外部回應、長時間沒事做。"""
    if aid == KANBAN:
        return
    await send_cmd({"agent_id": aid, "action": "use", "target": action})


async def goto_then_pose(aid: str, target: str, action: str) -> None:
    """走過去，【等真的走到】再擺姿勢。

    move_to 會清掉姿勢（見 pose 的說明），所以「送走位、立刻送姿勢」等於沒送——姿勢在人還在
    走的時候就被走路動畫蓋掉了。實際踩到：等審批的人走到老闆房門口後只是站著，講電話的動作
    從來沒出現過。

    背景跑（create_task）：呼叫端多半是 HTTP handler，不該為了等人走完路把回應卡住。
    走不到就不擺姿勢——那會讓動作出現在半路上，比沒有更怪。"""
    if aid == KANBAN or aid not in arrived:
        return
    arrived[aid].clear()
    if not await goto(aid, target):
        print(f"⚠ {aid} 走位指令沒送出（沒有畫面在線）——{target}／{action} 這段投影跳過")
        return
    try:
        await asyncio.wait_for(arrived[aid].wait(), timeout=30.0)
    except asyncio.TimeoutError:
        print(f"⚠ {aid} 走去 {target} 逾時沒回報抵達——不擺 {action}（動作出現在半路上更怪）")
        return
    if aid not in pending_approval and action == "phone":
        return   # 等待期間審批就結束了：不用再掏手機
    await pose(aid, action)


async def goto(aid: str, target: str) -> bool:
    """走位＋佔位登記（工作投影專用；生活迴圈有自己的佔位守衛）。
    回傳「指令有沒有真的送出去」——沒有畫面在線時 send_cmd 會靜靜回 False。

    呼叫端【幾乎都忽略】這個回傳值（走位失敗沒有補救動作，投影本來就是盡力而為），所以
    「沒送出去」必須在這裡就講出來，否則整條投影是無聲失效的：使用者看到「大家都杵著」，
    真正原因可能只是瀏覽器不支援 WebGL、canvas 根本沒載入。實際踩到。

    只在【狀態轉換】時印一次——離線期間每次都印會把 log 洗掉。"""
    global projection_offline
    if aid == KANBAN:   # 沒有身體的東西不會走路
        return False
    occupied[aid] = target
    ok = await send_cmd({"agent_id": aid, "action": "move_to", "target": target})
    if not ok and not projection_offline:
        projection_offline = True
        print("⚠ [投影] 沒有畫面在線（WebGL 未載入／分頁關了）——走位指令全數丟棄。"
              "工作串與看板不受影響，只有 3D 畫面不會動。")
    elif ok and projection_offline:
        projection_offline = False
        print("✓ [投影] 畫面回來了，走位恢復")
    return ok


# ── 投影泡泡節流：每個 NPC 一條佇列 + 播報器，最小間隔 BUBBLE_GAP。
# 工具泡（collapsible）連發時後浪蓋前浪只保最新；重要泡（任務/回報/收工）必播。
bubble_q: dict[str, list[tuple[str, bool]]] = {}   # aid -> [(text, collapsible)]
pacers: dict[str, asyncio.Task] = {}


async def bubble(aid: str, text: str, collapsible: bool = False) -> None:
    if aid == KANBAN:   # 沒有頭，就沒有頭上的泡泡（工作串照樣看得到）
        return
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
last_work: dict[str, float] = {}  # agent -> 最後一次有任務事件的時刻（久了就趴著睡）
last_tool: dict[str, float] = {}  # agent -> 最後一次【工具】事件的時刻（只有 think 在跑＝卡住）
watering: set[str] = set()        # 卡住而去飲水機的人（工具事件一回來就叫他回位）
sleeping: set[str] = set()        # 已經趴下的人：別每輪重送 move_to + sleep


def release_work(aid: str) -> None:
    """收工/失聯：釋放主 agent 與其名下所有委派卡（沒回報的委派標 lost，不留永久 working）。"""
    work_last.pop(aid, None)
    last_tool.pop(aid, None)
    watering.discard(aid)
    busy.discard(aid)
    for key in [k for k in sub_active if k[0] == aid]:
        for npc in sub_active.pop(key):   # 同一個鍵可能掛著多個並行子 agent，整批釋放
            busy.discard(npc)
            sub_since.pop(npc, None)
            close_card(npc, "lost")
    notify("roster")


async def sweep_work() -> None:
    now = time.monotonic()
    # 卡住＝上工中但一直沒有工具事件（模型長考、或在重試迴圈裡打轉）。think 事件本來完全
    # 不投影，於是「正在燒錢想事情」與「坐著發呆」畫面上一模一樣。
    # 只動【坐在自己位子上】的人：罰站等審批、站白板前、去別人桌邊的都不該被打斷。
    for aid in list(work_last):
        if aid in watering or aid in pending_approval:
            continue
        if occupied.get(aid) != WORK_DESK.get(aid):
            continue
        if now - last_tool.get(aid, now) > STUCK_AFTER:
            watering.add(aid)
            await goto(aid, COOLER)
            await bubble(aid, "● 思考中…")

    # 子 agent 的釋放事件掉了：主 agent 可能還活得好好的（work_last 一直在刷新），
    # 所以上面那條失聯規則救不到。這裡按【徵用時間】強制放人，否則那個 NPC 永遠不回座位。
    # 這是投影層的兜底——cogito 端已把釋放事件改成不可丟（見 office_reporter 的 isCritical），
    # 但橋長期掛掉時仍可能漏，且橋自己重啟也會失憶，所以兩邊都要有。
    for npc in [n for n, t in list(sub_since.items()) if now - t > SUB_TIMEOUT]:
        print(f"⚠ {npc} 被徵用 {SUB_TIMEOUT:.0f}s 未收到釋放事件，強制放人")
        sub_since.pop(npc, None)
        busy.discard(npc)
        for key, queue in list(sub_active.items()):
            if npc in queue:
                queue.remove(npc)
                if not queue:
                    sub_active.pop(key, None)
        close_card(npc, "lost")
        log_ev(npc, "⚠ 委派回報沒收到，自動收工")
        if desk := WORK_DESK.get(npc):
            await goto(npc, desk)   # 關鍵：把人送回座位，不然畫面上他就一直杵著
        notify("roster")

    for aid in [a for a, t in list(work_last.items()) if now - t > WORK_TIMEOUT]:
        print(f"⚠ {aid} 上工中 {WORK_TIMEOUT:.0f}s 無事件，視為失聯，釋放")
        release_work(aid)
        close_card(aid, "lost")
        notify("agent", aid, alert="error")
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
BUBBLE = {"start": "▶ 開工", "msg": "→ 回報"}

# 閒聊判別：問候／稱讚不是任務——不開任務卡、不起身走工位，只在現有工作串記一句。
# 用 Haiku 做意圖判斷（$1/$5 每百萬 token，一次十幾個 token 幾乎免費，且同句快取），
# 判不出來或沒有 API key 時退回關鍵字白名單。誤判方向刻意偏保守：拿不準一律當任務，
# 因為「把任務當閒聊」會讓人以為 agent 沒收到工作，比反過來糟得多。
INTENT_MODEL = "claude-haiku-4-5"
INTENT_SYSTEM = (
    "你在判斷老闆傳給 AI 員工的訊息屬於哪一類，只回一個英文單字。\n"
    "task＝交辦工作、提問、要求修改或補充資訊（即使很短，例如「整理週報」「跑一下測試」）。\n"
    "chat＝問候、稱讚、道謝、附和、確認收到，沒有要求做任何事。\n"
    "只輸出 task 或 chat，不要標點、不要解釋。"
)
_CHAT_WORD = (r"(?:nice job|good job|well done|nice work|thank you|thanks?|thx|nice|good|great|"
              r"awesome|cool|perfect|ok(?:ay)?|got it|辛苦了?|謝謝|感謝|做得好|做得不錯|太好了|"
              r"不錯|很棒|讚|沒問題|收到|了解|好的|👍|🎉)")
CHAT_RE = re.compile(rf"^\W*{_CHAT_WORD}(?:\W+{_CHAT_WORD})*\W*$", re.I)
_intent_cache: dict[str, bool] = {}
chat_mode: set[str] = set()   # 這一輪 run 是閒聊而非任務的 agent


async def is_chat(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 40:      # 長訊息不可能只是問候，省一次呼叫
        return False
    if t in _intent_cache:
        return _intent_cache[t]
    if client is None:            # 沒 API key：退回關鍵字白名單（合約測試走這條）
        return len(t) <= 24 and CHAT_RE.match(t) is not None
    try:
        r = await client.messages.create(
            model=INTENT_MODEL, max_tokens=5,
            system=INTENT_SYSTEM,
            messages=[{"role": "user", "content": t}],
        )
        reply = next((b.text for b in r.content if b.type == "text"), "")
        verdict = reply.strip().lower().startswith("chat")
    except Exception as e:
        print(f"⚠ 意圖判斷失敗（{type(e).__name__}），當成任務處理")
        verdict = False
    _intent_cache[t] = verdict
    return verdict


def office_bubble(kind: str, label: str) -> str | None:
    """事件 → NPC 頭上的狀態泡泡；None＝這種事件不冒泡。

    工具事件帶工具名（bash / read_file…）——每次做的事不一樣，泡泡才有資訊量；
    工具名是 ASCII，已烘進字型圖集。其餘用固定短語（中文字表有限）。
    """
    if kind == "done":
        return "✓ 完成" if label == "ok" else "⚠ 中斷"
    name = SUB_RE.sub("", label)  # 沒開成委派卡時 label 帶 [Subagent:名] 前綴，泡泡只顯示工具名
    if kind == "tool":
        return f"● {name[:14]}" if name else "● 執行中…"
    if kind == "error":
        return f"⚠ {name[:14]}" if name else "⚠ 出錯了"
    return BUBBLE.get(kind)


# 子 agent 投影（§十-1 第二刀）：spawn 具名 agent 映射到閒置 NPC 真走位——
# 阿哲委派 code-reviewer，小美就起身入座開工；內部事件泡泡掛到她頭上，收工冒回報泡。
# 沒人有空時退回舊行為（主 agent 頭上帶小名冒泡）。cogito / Unity 零改動。
# 慣用人選：具名子 agent 派給職務對得上的人（沒空就退回任一閒置者）
SUB_NPC = {"code-reviewer": "p01", "planner": "p01", "security-auditor": "p07",
           "implementer": "p12", "performance": "p05", "correctness": "p17"}
SPAWN_RE = re.compile(r"^spawn_subagent(?::(\S+))?")
# (主 agent, 子 agent 名) -> 演出的 NPC【清單】。
# 為什麼是清單而不是單一值：一次會議會並行派六個子 agent，未具名的 name 全部正規化成 ""，
# 具名的也可能同名並行——鍵一模一樣。原本存單一值，後派的就把先派的覆蓋掉，於是那些 NPC
# 連 release_work 都找不到，永遠停在「工作中」（實際踩到：會議跑完四個人卡住不動）。
# 收工時 FIFO 取一個：同名的兄弟對橋來說本來就分不出誰是誰，報告掛到哪一個是任意的，
# 但「每個都會被釋放」這件事是確定的——那才是重點。
sub_active: dict[tuple[str, str], list[str]] = {}


def npc_by_name(name: str) -> str | None:
    """用【人名】反查 persona id（小美 → p01）。"""
    return next((aid for aid, a in npcs().items() if a.name == name), None)


def pick_sub_npc(parent: str, name: str) -> str | None:
    free = [x for x in npcs() if x != parent and x not in busy]   # 看板沒有身體，不能被派去支援
    # 先用人名對名冊，再退回角色表。
    # kanban 頻道的具名 agent 用的是【人名】（小美、老徐…），SUB_NPC 那張表收的卻是【角色名】
    # （planner、implementer…）。只查角色表的話「派給小美」會落到隨便一個閒著的人身上——
    # 於是板子寫「👤 小美」、畫面上走過去的是阿海，兩邊各說各話。
    # 本人正忙就仍然退回別人代打：演出可以換角，但不能停演。
    cand = npc_by_name(name) or SUB_NPC.get(name)
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
            sub_active.setdefault((parent, name), []).append(npc)
            busy.add(npc)
            sub_since[npc] = time.monotonic()
            notify("roster")
            child = report_card(npc, f"支援{agents[parent].name}：{shown}")
            # 規劃類的活在白板前做（不佔工位，也讓「這是在想、不是在寫」看得出來）
            spot = BOARD if name in ("planner", "correctness") else WORK_DESK.get(npc)
            if spot:
                await goto(npc, spot)
            if side := DESK_SIDE.get(npc):   # 主 agent 走到對方桌邊站著看
                await goto(parent, side)
            await bubble(parent, "→ 交辦")
            await bubble(npc, "★ 支援中")
            # 委派事件掛上子卡引用 → 主任務串裡直接看得到對方在做什麼
            log_ev(parent, f"🤝 委派給 {agents[npc].name}：{shown}",
                   sub={"agent": npc, "id": child["id"]})
            log_ev(npc, f"📋 支援{agents[parent].name}：{shown}")
            agents[npc].remember(f"接下{agents[parent].name}委派的{shown}工作")
            return True
        if kind in ("result", "error"):  # 委派收工
            queue = sub_active.get((parent, name)) or []
            npc = queue.pop(0) if queue else None
            if not queue:
                sub_active.pop((parent, name), None)
            if npc is None:
                return False
            if desk := WORK_DESK.get(parent):   # 交接完主 agent 回自己位子繼續
                await goto(parent, desk)
            busy.discard(npc)
            sub_since.pop(npc, None)
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
        queue = sub_active.get((parent, sub.group(1) or "")) or []
        if not queue:
            return False  # 沒開成卡：退回主 agent 帶小名冒泡
        npc = queue[0]    # 同名並行時分不出是哪個兄弟送來的，一律掛第一個（見 sub_active 的說明）
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
            # day：卡片會跨天累積，只有 HH:MM 分不出「這是哪一天做的」（實際踩到）。
            # 不改 at 的格式——Unity 的報告面板與既有卡都吃它。
            "day": time.strftime("%Y-%m-%d"),
            "events": [], "at": time.strftime("%H:%M"), "end": ""}
    history.setdefault(aid, deque(maxlen=20)).append(card)
    last_report[aid] = card
    return card


def supersede_card(card: dict) -> None:
    """續跑接手舊卡：它與名下沒回報的委派卡都改標「已被續跑接手」。那不是失敗，是行程被砍掉的
    殘影——留著一片紅字失聯，會讓人以為工作真的斷在那裡沒人接。"""
    targets = [card]
    for e in card["events"]:
        ref = e.get("sub")
        child = find_card(ref["agent"], ref["id"]) if ref else None
        if child:
            targets.append(child)  # 子 agent 活在主 agent 的 run 裡，主的死了它必然也死了
    for c in targets:
        if c["status"] in ("working", "lost"):
            c["status"] = "superseded"
            c["end"] = c["end"] or time.strftime("%H:%M")


def close_card(aid: str, status: str) -> None:
    card = last_report.get(aid)
    if card and card["status"] == "working":
        card["status"] = status
        card["end"] = time.strftime("%H:%M")


# ── SSE 推播（失效通知模式）：只推「誰變了」，畫面收到再回抓既有 API——
# 單一資料權威（GET endpoints），不維護第二條全量資料路徑。
subscribers: set[asyncio.Queue] = set()
_dirty = False  # 有狀態變更待落地（存檔器每 2 秒巡）


def notify(kind: str, aid: str = "", alert: str = "") -> None:
    """alert＝要提示音的事件（approval／done／error）。刻意只有三種：使用者不會盯著畫面，
    但也不該每個工具呼叫都叮一聲——那跟沒有聲音一樣沒有資訊量。"""
    global _dirty
    _dirty = True  # 所有狀態變更都會走到 notify——持久化搭同一班車
    ev = {"type": kind, "id": aid}
    if alert:
        ev["alert"] = alert
    for q in list(subscribers):
        if q.qsize() < 100:  # 塞爆代表客戶端死了，丟事件等它斷線清理
            q.put_nowait(ev)


CHAT_MARK = "💬"   # /office/chat 那條路會帶這個前綴；/office/event(msg) 不帶


TURN_QUIET = 12.0                       # 距上一則事件超過這麼久，那段空白才值得標「思考中」
last_ev_at: dict[str, float] = {}       # aid -> 最後一則事件的時間（monotonic）

NOTE_MARK = "🧑‍💼 老闆交辦："


def note_body(note: str) -> str:
    """剝掉「老闆交辦：」前綴，只留老闆真正說的那句話。"""
    return note.removeprefix(NOTE_MARK).strip()


def body_of(text: str) -> str:
    """去掉投影用的前綴符號，只留內容——同一句話經不同路徑會帶不同前綴（💬 / 無）。"""
    return text.lstrip("💬 ").strip()


_SIG_DROP = str.maketrans("", "", "*#`|-—_> \t\n\r：:，,。.（）()「」【】")


def sig(text: str, n: int = 40) -> str:
    """內容指紋：抽掉所有排版符號與空白，只留「說了什麼」。

    為什麼不能直接比文字：cogito 會【針對平台改寫排版】——同一段話，chat 那條路送
    `**🟢 收尾類**` ＋ ``` 程式碼區塊，event 那條送 `## 🟢 收尾類` ＋ markdown 表格。
    實測那兩份在第 44 個字就分岔，比前綴永遠比不出來（我原本假設兩邊文字相同，那個假設是錯的）。
    去掉排版之後剩下的字才是同一份。
    """
    return body_of(text).translate(_SIG_DROP)[:n]


def dup_of_last(aid: str, text: str) -> bool:
    """這段文字是不是同一則訊息走【另一條路】又送了一次。

    規則刻意精準：內容相同【而且前綴狀態不同】（一個帶 💬、一個不帶）才算重複。
    那正是兩條路徑雙胞胎的指紋——`/office/chat` 帶 💬，`/office/event(msg)` 不帶。

    先前用「長度超過 60 才去重」當門檻，短訊息（實測踩到一句 38 字的）就漏掉了。
    長度門檻本來就是錯的抽象：它想擋的是「✓ bash」「第 5 輪」那種會正常重複的記號，
    但那些兩邊都不帶 💬，用前綴狀態就分得乾淨，跟長短無關。

    截斷長度兩條路不同（300 vs 200），所以比【較短那段的前綴】而不是整段。
    """
    card = last_report.get(aid)
    if not card or not card.get("events"):
        return False
    mine, is_chat = sig(text), text.lstrip().startswith(CHAT_MARK)
    if len(mine) < 12:               # 指紋太短（「✓ bash」之類）無從分辨，交給前綴狀態就好
        return False
    for ev in card["events"][-3:]:   # 只看最近幾則：更早的同句話是真的又說了一次
        prev = ev.get("text", "")
        if prev.lstrip().startswith(CHAT_MARK) == is_chat:
            continue                 # 前綴狀態一樣＝同一條路來的，那是真的重複發言
        if sig(prev) == mine:
            return True
    return False


def log_ev(aid: str, text: str, sub: dict | None = None) -> None:
    """sub＝{agent, id}：這是一則委派事件，前端在此處內嵌對方的子任務卡。

    去重擋在這裡而不是某一條呼叫路徑上：同一則助理訊息會經 /office/chat 與 /office/event(msg)
    兩條路送來，先前只擋了「chat 比對既有事件」單一方向——先到的是 chat 時，後到的 msg 照樣
    寫進去，畫面上還是兩份。擋在共用入口才沒有方向問題（也不必每個新來源記得自己擋）。"""
    if sub is None and dup_of_last(aid, text):
        return
    card = last_report.get(aid)
    if card is None:   # 沒有任何卡可掛（例：全新員工的第一則系統訊息）→ 開一張雜記卡。
        card = report_card(aid, "（雜項）")
        card["status"] = "note"   # 它不是任務，別掛「進行中」——那會永遠轉下去
    last_ev_at[aid] = time.monotonic()   # 給回合標記判斷「這段空白是不是真的」
    ev: dict = {"at": time.strftime("%H:%M:%S"), "text": text}
    if sub:
        ev["sub"] = sub
    card["events"].append(ev)
    if len(card["events"]) > MAX_CARD_EVENTS:
        del card["events"][0]
    notify("agent", aid)


# 寫檔工具的參數就是產出本身：拆成「檔名 + 程式碼區塊」，比一行 JSON 好讀得多。
# 副檔名對應 markdown 圍欄的語言標籤；沒列到的就不標（區塊照樣是等寬可捲的）。
WRITE_TOOLS = ("write_file", "edit_file")
LANGS = {"py": "python", "js": "javascript", "ts": "typescript", "go": "go", "sh": "bash",
         "html": "html", "css": "css", "json": "json", "yaml": "yaml", "yml": "yaml",
         "md": "markdown", "sql": "sql", "cs": "csharp", "rs": "rust", "java": "java"}
WRITE_BODY_MAX = 2000   # 與 cogito 端的 writeArgsMax 對齊；超長只顯示前段


def write_tool_text(label: str, detail: str) -> str | None:
    """write_file/edit_file 的參數 → 「▸ write_file · 檔名」＋程式碼區塊。解不開就回 None 走原路。"""
    try:
        args = json.loads(detail)
    except ValueError:
        return None      # 被截斷的 JSON 解不開很正常（超長檔案）——退回原本那行，不要吞掉事件
    if not isinstance(args, dict):
        return None
    path = args.get("path") or args.get("file") or ""
    body = args.get("content") or args.get("new_string") or args.get("new") or ""
    if not isinstance(body, str) or not body.strip():
        return None
    lang = LANGS.get(path.rsplit(".", 1)[-1].lower(), "") if "." in path else ""
    body = body[:WRITE_BODY_MAX] + ("\n…（內容過長，只顯示前段）" if len(body) > WRITE_BODY_MAX else "")
    head = f"▸ {label}" + (f" · {path}" if path else "")
    return f"{head}\n```{lang}\n{body}\n```"


def tl_text(kind: str, label: str, detail: str) -> str | None:
    """事件 → 時間軸一行；None＝不記（think/turn）。比泡泡完整（帶參數/結果預覽）。"""
    if kind == "tool":
        if label in WRITE_TOOLS and detail:
            if (block := write_tool_text(label, detail)) is not None:
                return block
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
    free = [x for x in npcs() if x not in busy and x not in conv_npc.values()]
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
    last_work[aid] = time.monotonic()  # 有事件＝這位還在做事，重新計算「閒多久」
    sleeping.discard(aid)              # 睡著的被叫醒（下一輪就恢復正常走動）
    if kind in ("start", "tool", "result", "error"):   # 有實質進展（think/turn 不算）
        last_tool[aid] = time.monotonic()
        if aid in watering:            # 卡住的人有進展了：回位子繼續
            watering.discard(aid)
            if desk := WORK_DESK.get(aid):
                await goto(aid, desk)

    if await project_sub(aid, kind, label, ev.get("detail", "")):
        return {"ok": True}

    # 審批逾時（自動拒絕）後工作恢復：人還杵在老闆房門口，看到工具事件就自己回工位
    if kind == "tool" and aid not in pending_approval and occupied.get(aid) == BOSS_DOOR:
        if desk := WORK_DESK.get(aid):
            await goto(aid, desk)

    chatting = aid in chat_mode  # 這一輪是閒聊：不開卡、不走位、不冒任務泡
    if kind == "start":
        busy.add(aid)
        notify("roster")
        if await is_chat(label) and last_report.get(aid):
            # 閒聊（「nice job」「辛苦了」）不是任務：不開卡、不起身走工位，
            # 只在目前這張卡的串裡記一句對話——回一句問候卻被當成新任務很怪。
            chat_mode.add(aid)
            chatting = True
            await pose(aid, "face_down")   # 有人在跟他講話：轉頭面向鏡頭（原本背對）
            log_ev(aid, f"💬 老闆：{label}")
        else:
            # 斷點續跑：cogito 送的 prompt 是那句系統提示，拿它當卡片標題沒人看得懂做過什麼。
            # 沿用上一張未完成卡的任務名，才接得回中斷前那條工作串。
            task = label
            if label.startswith(RESUME_NUDGE_PREFIX):
                prev = next((t for t in reversed(history.get(aid, []))
                             if t["status"] in ("working", "lost", "superseded")
                             and not t["task"].startswith(RESUME_NUDGE_PREFIX)), None)  # 舊格式的續跑卡跳過
                task = ("🔄 續跑：" + prev["task"].removeprefix("🔄 續跑：")) if prev else "🔄 續跑上次中斷的任務"
                if prev:
                    supersede_card(prev)
            report_card(aid, task, ev.get("detail", ""))  # start 的 detail＝工作目錄
            log_ev(aid, f"📋 接到任務：{task}")
            # 派工那行掛回它要開始的任務。但【內容相同就不重覆記】——多數情況下卡片標題
            # 就是老闆那句話，兩行並排只是同一段文字說兩次。只有續跑（標題被換成「🔄 續跑：」）
            # 或 cogito 改寫過任務名時，這行才帶來新資訊。
            note = pending_note.pop(aid, None)
            if note and note_body(note)[:40] != task.strip()[:40]:
                log_ev(aid, note)
            if desk := WORK_DESK.get(aid):
                await goto(aid, desk)
            a.remember(f"接到工作任務「{label}」，開始上工")
    elif kind == "turn":
        # 回合標記【只在真的安靜時】才記。它原本的用途是填補長考的空白——兩次工具呼叫之間
        # 如果什麼都沒有，看起來像 agent 掛了。但每輪都印就變成另一種噪音：使用者看到的是
        # 一連串「第 N 輪」夾在有內容的事件中間，而「第幾輪」對他毫無意義（實際回報）。
        # 只有距離上一則事件超過 TURN_QUIET 秒，那段空白才是真的，這行才有資訊。
        quiet = time.monotonic() - last_ev_at.get(aid, 0.0)
        if aid not in chat_mode and last_report.get(aid) and quiet > TURN_QUIET:
            log_ev(aid, f"⋯ 思考中（第 {label} 輪）")
    elif kind == "msg":
        card = last_report.get(aid) or report_card(aid, "（橋重啟，任務開頭沒記到）")
        if aid not in chat_mode:  # 閒聊的回話不覆蓋任務的報告全文
            card["report"] = label
        log_ev(aid, label[:200])
    elif kind == "done":  # 收工：釋放主 agent＋名下委派卡，回歸 idle
        if chatting and (desk := WORK_DESK.get(aid)):
            await goto(aid, desk)   # 閒聊結束：轉回去繼續坐著（move_to 會清掉轉頭的姿勢）
        chat_mode.discard(aid)
        if not chatting:  # 閒聊不改任務卡狀態（那張卡早就完成了）
            close_card(aid, "ok" if label == "ok" else "error")
            card = last_report.get(aid)
            if card and label != "ok" and ev.get("detail"):
                card["report"] = card["report"] or ev["detail"]
        pending_approval.pop(aid, None)  # 任務結束，殘留審批卡（逾時自動拒絕）一併收掉
        approval_src.pop(aid, None)
        release_work(aid)
        if not chatting:
            notify("agent", aid, alert="done" if label == "ok" else "error")
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

    # 閒聊：接到/收工不是任務事件，不冒泡；回話（msg）照樣冒——那是員工在回應老闆
    text = None if (chatting and kind in ("start", "done")) else office_bubble(kind, label)
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
            "approval_from": approval_from(aid),  # 非空＝要回該平台核准，外殼不給按鈕
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
RESUME_NUDGE_PREFIX = "[系統] 先前因暫時性錯誤"            # core.go resumeNudge：斷點續跑的系統提示
PROGRESS_PREFIXES = ("🤔", "🛠️", "✅ *執行成功*", "⚠️ *執行報錯*")  # OfficeReporter 已投影過，去重
pending_approval: dict[str, str] = {}  # npc id -> 待審批卡文字（shell 顯示核准/駁回按鈕）
pending_note: dict[str, str] = {}     # npc id -> 等下一張任務卡開出來才掛上去的「老闆交辦」
# npc id -> 這張審批來自哪個頻道（office:p17 / slack:C999…）。非 office 來源只能回原平台核准：
# cogito 的審批是 ResolveByChannel 按頻道解析的，而這裡的 approve 一律送往 office:pXX，
# 送過去只會得到「當前沒有待審批的操作」，卡卻已經收掉——看起來像批准成功，其實對方還在等逾時。
approval_src: dict[str, str] = {}
PLATFORM_NAME = {"slack": "Slack", "telegram": "Telegram", "office": "",
                 # COGITO_USER_LINK 把跨平台私訊歸一成 user:<canonical> 時，來源就只剩這個前綴，
                 # 已經看不出是 Slack 還是 TG——寫成「原平台」比讓畫面出現「請回 user 核准」好。
                 "user": "原平台"}


def approval_from(aid: str) -> str:
    """回傳審批來源平台的顯示名；空字串＝office 本地，可以直接按核准。"""
    plat = approval_src.get(aid, "office").split(":", 1)[0]
    return PLATFORM_NAME.get(plat, plat)


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
        approval_src[aid] = ev.get("agent", "")
        # 等審批也算工作中：不標 busy 的話生活 idle 迴圈會把罰站走位蓋掉
        busy.add(aid)
        work_last[aid] = time.monotonic()
        notify("roster", aid, alert="approval")   # 最需要抬頭的一件事：有人在等你決定
        # 走到老闆房門口站著等（門口真的有人在等就原地等，不擠）。
        # 球在別人手上：講電話，不是站著發呆——但姿勢必須【走到之後】才擺，否則被走路動畫蓋掉。
        #
        # 「有沒有人佔著」要問【現在誰真的在等審批】，不能查 occupied：那張表只會被同一個人的
        # 下一次走位覆蓋，從不釋放。只要有誰的任務在門口失聯/逾時，他就永遠佔著那個點，
        # 之後每一個要過去的人都被幽靈擋住——實際踩到：HITL 觸發了，人卻完全沒動。
        crowded = any(a != aid and occupied.get(a) == BOSS_DOOR for a in pending_approval)
        if not crowded:
            asyncio.create_task(goto_then_pose(aid, BOSS_DOOR, "phone"))
        else:
            await pose(aid, "phone")   # 原地等：沒有走位，就不必等抵達
        await bubble(aid, "⚠ 等待審批")
    log_ev(aid, f"💬 {text[:300]}")   # 重複由 log_ev 統一擋（同一則訊息會走兩條路送來）
    return {"ok": True}


# 能力清單（工具／技能）：問 cogito 本人，不在橋這邊寫死——工具是按頻道組裝的
# （MCP、背景任務、自我進化都是條件式掛載），寫死的表遲早跟現實不符。
#
# 這是【全員共用】的清單，不是某個人的屬性：工具在各頻道各自 rooted 到自己的工作目錄，
# 但掛上的是同一組；技能更是全 bot 讀同一份 .claw/skills。所以挑任一位員工去問即可。
# 快取到行程結束：同一個 bot 跑著的期間清單不會變。
_caps_cache: dict | None = None


@app.get("/office/caps")
async def office_caps():
    global _caps_cache
    if _caps_cache:
        return _caps_cache
    if not COGITO_HTTP:
        return {"ok": False, "error": "未設 COGITO_HTTP——問不到 cogito 的能力清單"}
    probe = next(iter(agents), "")   # 清單全員相同，隨便挑一位當探針
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            r = await cl.get(f"{COGITO_HTTP}/capabilities", params={"agent": probe},
                             headers={"Authorization": f"Bearer {COGITO_HTTP_TOKEN}"})
        r.raise_for_status()
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "error": f"取不到能力清單：{type(e).__name__}"}
    # mcp：外部 MCP 工具不個別註冊（cogito 只掛 mcp_call_tool／mcp_describe_tool 兩個閘道），
    # 所以清單得從 gateway 的目錄另外拿——否則看板上只看得到兩個閘道，看不出實際掛了什麼。
    _caps_cache = {"ok": True, "tools": d.get("tools") or [], "skills": d.get("skills") or [],
                   "mcp": d.get("mcp") or []}
    return _caps_cache


@app.get("/office/profile/{aid}")
def office_profile(aid: str):
    """點名冊看「這位員工是誰」：persona 欄位 + 同名 .md 的角色設定（soul）。
    單一事實來源是 backend/personas/——畫面只是把它讀出來，不另存一份。"""
    a = agents.get(aid)
    if a is None:
        return {"ok": False, "error": "查無此員工"}
    soul = Path(__file__).parent / "personas" / f"{aid}.md"
    return {"ok": True, "id": aid, **{k: a.persona.get(k, "") for k in
            ("name", "role", "team", "personality", "style", "habits")},
            "soul": soul.read_text(encoding="utf-8") if soul.exists() else ""}


# ── 產出預覽：把任務卡的工作目錄唯讀開一個窗，讓圖片／PDF 直接在工作串裡看得到。
# ⚠ 這是唯一會把磁碟內容送出去的地方，三道界線：
#   ①路徑一律以「卡號查出來的工作目錄」為根，客戶端只能給相對路徑；解析後必須仍在根底下（擋 ../）
#   ②副檔名白名單；③大小上限。HTML 一律以純文字送出（見 PREVIEW_TYPES 的註解）。
PREVIEW_MAX = 5 << 20   # 5 MB：再大的產物請開資料夾看
PREVIEW_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
    "webp": "image/webp", "svg": "image/svg+xml", "pdf": "application/pdf",
    # 文字類一律 text/plain：HTML/JS 若以 text/html 送出，就是在橋的來源上執行 agent 產生的程式碼
    # （agent 會被 prompt injection 影響），這條線先不跨——要真的預覽網頁見「沙箱預覽」的討論。
    **{e: "text/plain; charset=utf-8" for e in
       ("txt", "md", "py", "js", "ts", "go", "html", "css", "json", "yaml", "yml", "sh", "csv", "sql")},
}


def card_dir(aid: str, cid) -> Path | None:
    wd = (find_card(aid, cid) or {}).get("workdir", "")
    return Path(wd) if wd and Path(wd).is_dir() else None


def agent_dir(aid: str) -> Path | None:
    """這位員工的工作區根目錄：優先用最近一張帶 workdir 的卡（那是 cogito 自己報的路徑），
    沒有卡就退回 COGITO_CHANNELS/office_<aid>（還沒接過任務的新人也看得到自己的資料夾）。"""
    for card in reversed(history.get(aid, [])):
        wd = card.get("workdir", "")
        if wd and Path(wd).is_dir():
            return Path(wd)
    if CHANNELS_DIR:
        d = CHANNELS_DIR / f"office_{aid}"
        if d.is_dir():
            return d
    return None


def resolve_in(base: Path, rel: str) -> Path | None:
    """把客戶端給的相對路徑鎖在 base 底下（解析後必須仍在 base 內，擋 ../ 與符號連結）。"""
    try:
        f = (base / rel).resolve() if rel else base.resolve()
        f.relative_to(base.resolve())
        return f
    except (ValueError, OSError):
        return None


# 基礎設施檔：橋自己同步進工作區的東西，不是 agent 的產出。列檔的兩條路徑（卡片產出清單、
# 工作區瀏覽）共用這個判斷——兩邊各寫一份的話，改一邊忘另一邊只是時間問題。
# 只在【工作區根目錄】適用：子目錄裡的同名檔是 agent 自己寫的，那就是產出。
INFRA_FILES = {"AGENTS.md"}


def infra_file(name: str) -> bool:
    return name in INFRA_FILES


def listing(base: Path, rel: str) -> dict:
    """列一層目錄：資料夾在前、檔名排序；隱藏檔跳過；不在白名單的副檔名只列不給預覽。"""
    d = resolve_in(base, rel)
    if d is None or not d.is_dir():
        return {"ok": False, "error": "路徑越界或不是資料夾"}
    base = base.resolve()   # iterdir() 給的是實體路徑；macOS 的 /var→/private/var 會讓
    dirs, files = [], []    # relative_to 對不上（測試抓到的）
    for f in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if f.name.startswith(".") or (not rel and infra_file(f.name)):
            continue
        r = str(f.relative_to(base))
        if f.is_dir():
            dirs.append({"name": f.name, "path": r, "dir": True})
        else:
            ext = f.suffix.lstrip(".").lower()
            files.append({"name": f.name, "path": r, "dir": False, "ext": ext,
                          "size": f.stat().st_size,
                          "kind": "image" if PREVIEW_TYPES.get(ext, "").startswith("image")
                                  else "pdf" if ext == "pdf"
                                  else "text" if ext in PREVIEW_TYPES else "raw"})
        if len(dirs) + len(files) >= 200:   # 目錄爆量時只列前 200 筆
            break
    up = str(Path(rel).parent) if rel and str(Path(rel).parent) != "." else ("" if rel else None)
    return {"ok": True, "path": rel, "up": up, "entries": dirs + files}


def serve_file(base: Path, rel: str, render: int):
    f = resolve_in(base, rel)
    if f is None:
        return {"ok": False, "error": "路徑越界"}
    ext = f.suffix.lstrip(".").lower()
    if not f.is_file() or ext not in PREVIEW_TYPES:
        return {"ok": False, "error": "不支援預覽這種檔案"}
    if f.stat().st_size > PREVIEW_MAX:
        return {"ok": False, "error": f"檔案超過 {PREVIEW_MAX >> 20} MB，請開資料夾看"}
    headers = {"X-Content-Type-Options": "nosniff",
               "Content-Disposition": f'inline; filename="{f.name}"'}
    media = PREVIEW_TYPES[ext]
    if render and ext in ("html", "svg"):
        media = "text/html; charset=utf-8" if ext == "html" else media
        headers["Content-Security-Policy"] = "sandbox allow-scripts"
    return FileResponse(f, media_type=media, headers=headers)


@app.delete("/office/history/{aid}")
def office_clear_day(aid: str, day: str = "", scope: str = "day"):
    """清掉任務卡。scope=day（預設）只清某一天（day=""＝沒有日期欄位的舊卡）；scope=all 清全部。

    ⚠ 進行中的卡不刪：那是還在跑的任務，刪了畫面與事件流就對不上（事件還會繼續進來，
    又會被 log_ev 開一張新的雜項卡）。等它收工再清。
    """
    cards = history.get(aid)
    if not cards:
        return {"ok": False, "error": "這位員工沒有工作紀錄"}
    keep, removed, skipped = [], 0, 0
    for t in cards:
        if scope != "all" and t.get("day", "") != day:
            keep.append(t)
        elif t["status"] == "working":
            keep.append(t)
            skipped += 1
        else:
            removed += 1
    if not removed:
        where = "沒有可清的卡" if scope == "all" else "那一天沒有可清的卡"
        return {"ok": False, "error": where + ("（都還在進行中）" if skipped else "")}
    history[aid] = deque(keep, maxlen=20)
    # 最新卡指標要跟著移動；整串被清空就連指標一起收掉
    if keep:
        last_report[aid] = keep[-1]
    else:
        last_report.pop(aid, None)
    notify("agent", aid)
    return {"ok": True, "removed": removed, "skipped": skipped, "left": len(keep)}


@app.get("/office/ws/{aid}")
def office_ws(aid: str, p: str = ""):
    """員工工作區瀏覽：可進子目錄（卡片的產出清單只列第一層，那是給任務看的）。"""
    base = agent_dir(aid)
    if base is None:
        return {"ok": False, "error": "這位員工還沒有工作區"}
    r = listing(base, p)
    if r.get("ok"):
        r["root"] = base.name
    return r


# ── 看板投影：把協作任務的狀態畫成四欄。
# 資料來源是 kanban 頻道工作區裡的 board.json——【agent 寫、人只看】。所以這裡沒有拖拉、沒有
# 版本號、沒有並發控制：卡片是被主持人移動的，不是被使用者拖的。
#
# 「等待相依」不是一個要 agent 自己維護的狀態，而是【算出來的】：todo 且相依還沒全部完成＝等待。
# 少一個狀態就少一種寫錯的可能——agent 只要維護 todo/doing/done 三種。
BOARD_COLUMNS = [("todo", "待辦"), ("blocked", "等待相依"), ("doing", "進行中"), ("done", "完成")]


def board_column(task: dict, done_ids: set[str]) -> str:
    st = task.get("status", "todo")
    if st in ("doing", "done"):
        return st
    deps = task.get("deps") or []
    return "blocked" if any(d not in done_ids for d in deps) else "todo"


@app.get("/office/board")
def office_board():
    """看板快照。沒有 board.json 就回 ok=False——協作還沒開始，面板整個不顯示。"""
    base = agent_dir(KANBAN)
    if base is None:
        return {"ok": False, "error": "看板還沒有工作區"}
    f = base / "board.json"
    if not f.exists():
        return {"ok": False, "error": "還沒有進行中的協作任務"}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"board.json 讀不動：{type(e).__name__}"}
    tasks = data.get("tasks") or []
    done_ids = {t.get("id") for t in tasks if t.get("status") == "done"}
    cols = {k: [] for k, _ in BOARD_COLUMNS}
    for t in tasks:
        col = board_column(t, done_ids)
        # owner 是 persona id 的話換成名字——板子上要看得懂是誰，不是 p07
        owner = t.get("owner")
        cols[col].append({
            "id": t.get("id", ""), "title": t.get("title") or t.get("id", ""),
            "deps": t.get("deps") or [],
            "owner": agents[owner].name if owner in agents else owner,
            "out": t.get("out"),
        })
    # live＝主持人現在真的在推進這塊板。board.json 只有主持人在跑時才會更新，它一收工
    # 「進行中」那欄就永遠停在原地——畫面上看起來有人在做，實際沒有。投影不能說謊：
    # 板子是不是活的，橋知道，就要講出來。
    return {"ok": True, "task": data.get("task", ""), "live": KANBAN in busy,
            "columns": [{"key": k, "name": n, "cards": cols[k]} for k, n in BOARD_COLUMNS]}


@app.delete("/office/board")
def office_board_clear():
    """收掉目前這塊板。改名成 board.<時間>.json 而不是刪除——那是一次協作的完整紀錄，
    主持人也可能還想回頭看；真的不要了再自己去工作區刪。"""
    base = agent_dir(KANBAN)
    f = base / "board.json" if base else None
    if not f or not f.exists():
        return {"ok": False, "error": "目前沒有板子"}
    dst = f.with_name(f"board.{time.strftime('%m%d-%H%M%S')}.json")
    f.rename(dst)
    notify("agent", KANBAN)
    return {"ok": True, "archived": dst.name}


@app.get("/office/wsfile/{aid}")
def office_wsfile(aid: str, p: str, render: int = 0):
    base = agent_dir(aid)
    if base is None:
        return {"ok": False, "error": "這位員工還沒有工作區"}
    return serve_file(base, p, render)


@app.get("/office/files/{aid}/{cid}")
def office_files(aid: str, cid: int):
    """任務卡的產出清單（只列第一層、只列白名單副檔名、最多 40 筆）。"""
    base = card_dir(aid, cid)
    if base is None:
        return {"ok": False, "error": "這張卡沒有產出目錄"}
    out = []
    for f in sorted(base.iterdir()):
        ext = f.suffix.lstrip(".").lower()
        if not f.is_file() or f.name.startswith(".") or ext not in PREVIEW_TYPES or infra_file(f.name):
            continue
        out.append({"name": f.name, "size": f.stat().st_size, "ext": ext,
                    "kind": "image" if PREVIEW_TYPES[ext].startswith("image") else
                            "pdf" if ext == "pdf" else "text"})
        if len(out) >= 40:
            break
    return {"ok": True, "files": out}


@app.get("/office/file/{aid}/{cid}")
def office_file(aid: str, cid: int, p: str, render: int = 0):
    """render=1 且是 HTML → 以 text/html 送出，供【沙箱 iframe】渲染。
    這條路等於執行 agent 寫的程式碼，所以：①必須由使用者顯式點下去（前端不預設渲染）
    ②回應帶 CSP sandbox allow-scripts——就算 iframe 的 sandbox 屬性被漏掉，瀏覽器仍以
    不透明來源執行它（拿不到本站 cookie/localStorage、不能發同源請求）。"""
    base = card_dir(aid, cid)
    if base is None:
        return {"ok": False, "error": "這張卡沒有產出目錄"}
    return serve_file(base, p, render)


@app.post("/office/open")
def office_open(d: dict):
    """點任務卡的 📁 → 用系統檔案總管打開那個產出目錄（產出多半是一整包檔案，
    逐個下載沒意義，直接進資料夾比較快）。
    只吃 agent + 卡號、路徑由橋自己查——不接受客戶端傳路徑，否則這就是個任意路徑開啟器。"""
    card = find_card(d.get("agent", ""), d.get("card"))
    wd = (card or {}).get("workdir", "")
    if not wd or not Path(wd).is_dir():
        return {"ok": False, "error": "這張卡沒有產出目錄（或目錄已不在）"}
    try:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", wd])
    except OSError as e:
        return {"ok": False, "error": f"開啟失敗：{e}"}
    return {"ok": True, "workdir": wd}


@app.get("/office/status")
def office_status():
    """畫面/派工鏈路健康度——WebGL 分頁被瀏覽器凍結時 WS 仍開著，指令會送進黑洞，
    這個旗標讓外殼直接顯示「畫面未連線」而不是讓人猜為什麼 NPC 不動。"""
    return {"canvas": len(viewers), "stale": canvas_stale,
            "live": bool(loops), "dispatch": bool(COGITO_HTTP),
            # SSE 訂閱數：接近 6 就代表瀏覽器的連線額度快被背景分頁吃光（fetch 會開始無限排隊）
            "streams": len(subscribers)}


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
    verb = text.split()[0]
    # 防呆：工作中不收新任務（cogito 也會擋，這裡先給即時回饋）。
    # approve/reject/stop 是任務【進行中】的互動，一律放行——擋住中止等於沒有中止。
    if verb in ("approve", "reject") and (src := approval_from(aid)):
        return {"ok": False, "error": f"這張審批來自 {src}，請回 {src} 核准（cogito 按頻道解析審批）"}
    if aid in busy and verb not in ("approve", "reject", "/stop"):
        return {"ok": False, "error": f"{agents[aid].name} 正在工作中，收工後再派新任務"}
    if not COGITO_HTTP:
        return {"ok": False, "error": "未設 COGITO_HTTP——cogito 的 HTTP 派工入口未啟用"}
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            # 中止時若正卡在審批：【先送駁回再送中止】。agent 這時阻塞在等審批，中止指令它根本
            # 讀不到，要等五分鐘逾時自動拒絕才會醒——使用者眼裡就是「按了中止卻還卡在選擇上」。
            # 語意上也對：要停掉整件事，那個高危操作當然不該放行。
            if verb == "/stop" and aid in pending_approval:
                await cl.post(f"{COGITO_HTTP}/task", json={"agent": aid, "text": "reject"},
                              headers={"Authorization": f"Bearer {COGITO_HTTP_TOKEN}"})
                pending_approval.pop(aid, None)
                approval_src.pop(aid, None)
                log_ev(aid, "🧑‍💼 中止前先駁回了待審批的操作")
            r = await cl.post(f"{COGITO_HTTP}/task", json={"agent": aid, "text": text},
                              headers={"Authorization": f"Bearer {COGITO_HTTP_TOKEN}"})
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"cogito 入口連不上：{type(e).__name__}"}
    if r.status_code != 202:
        return {"ok": False, "error": f"cogito 回 {r.status_code}：{r.text[:120]}"}
    if verb == "/stop":
        # cogito 的 /stop 本來就吃得下（忙碌時照樣消費），這裡只補投影：泡泡＋工作串留痕。
        # 真正收卡等 cogito 的 done 事件——中止要等目前這一步（模型呼叫或工具）跑完才生效。
        log_ev(aid, "🧑‍💼 老闆要求中止這個任務")
        await bubble(aid, "⚠ 中斷")
    elif verb in ("approve", "reject"):
        pending_approval.pop(aid, None)  # cogito 確認收到才收卡
        approval_src.pop(aid, None)
        notify("agent", aid, alert="done")   # 決定送出去了：給個回饋，不然按完毫無反應
        await bubble(aid, "✓ 放行" if verb == "approve" else "⚠ 駁回")
        log_ev(aid, f"🧑‍💼 老闆{'核准' if verb == 'approve' else '駁回'}了這個操作")
        if desk := WORK_DESK.get(aid):  # 審批完回工位繼續
            await goto(aid, desk)
    else:
        if not await is_chat(text):  # 閒聊由 start 事件記成 💬 對話，這裡不重複
            # ⚠ 不能直接 log_ev：這一刻上一張卡已經收了、cogito 的 start 還沒到，
            # log_ev 會為了放這行字開一張「（雜項）」卡並掛成「進行中」，永遠不會關。
            # 寄放著，等 start 開出真的任務卡再掛進去——這行本來就屬於它要開始的那件事。
            pending_note[aid] = f"{NOTE_MARK}{text[:200]}"
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
              "approval": aid in pending_approval,   # 等你決定的人要在名冊上一眼看得到
              "npc": aid != KANBAN,   # False＝這張卡沒有身體（看板），前端不畫走位/位置
              "memory": list(a.memory)}
        for aid, a in agents.items()
    }


# ── 持久化：任務卡/黏性指派/審批卡落地 JSON——橋重啟不失憶（cogito 的 session 本來就落地，
# 這邊補齊對稱）。notify() 兼作 dirty 標記，存檔器每 2 秒批次寫（原子寫入：tmp + rename）。
STATE_FILE = Path(os.environ.get("OFFICE_STATE") or Path(__file__).parent / "office_state.json")

# ── 人設落地：把 personas/<id>.md 同步進 cogito 各頻道的工作目錄當 AGENTS.md。
# cogito 的 PromptComposer 會把工作目錄裡的 AGENTS.md 讀進系統提示，所以這一步讓角色設定
# 【真的影響 agent 行為】，而不只是名冊上的一張名片。沒設 COGITO_CHANNELS 就整個不啟用——
# 這是往別的 repo 的工作區寫檔，預設關閉。
CHANNELS_DIR = Path(os.environ["COGITO_CHANNELS"]).expanduser() if os.environ.get("COGITO_CHANNELS") else None
SOUL_MARK = "<!-- office-persona:"   # 我們產生的檔案的第一行；手寫的 AGENTS.md 沒有它


def soul_doc(aid: str, body: str) -> str:
    p = agents[aid].persona
    head = "\n".join(x for x in [
        f"# 你是{p.get('name', aid)}（{p.get('role', '員工')}）",
        f"\n個性：{p.get('personality', '')}" if p.get("personality") else "",
        f"說話風格：{p.get('style', '')}" if p.get("style") else "",
    ] if x)
    return (f"{SOUL_MARK} {aid} 由 backend/personas/{aid}.md 產生。手改會在橋下次啟動時被覆蓋；\n"
            f"     想自己維護這個檔案，把這兩行標記刪掉即可，橋就不會再動它。 -->\n\n"
            f"{head}\n\n{body.strip()}\n")


# 子 agent 的工具集是 opt-in 的：具名 agent 沒宣告 tools 就只拿到唯讀探路者子集
# （read_file + bash，見 cogito 的 defaultSubagentTools）。所以【不宣告＝實作工作永遠派不出去】，
# 主持人只能自己做——板子上的 owner 全成了裝飾，Unity 也不會有人走動（沒有委派就沒有投影）。
#
# 實作型的人給寫入，決策型的維持唯讀：小美收範圍、老徐把關，他們的產出是判斷不是檔案。
# 寫入照樣過審批 middleware，這裡放行的是「能不能被派這種工作」，不是「能不能繞過稽核」。
READONLY_TOOLS = ["read_file", "bash"]                             # 決策型：只讀，產出是判斷
WRITER_TOOLS = ["read_file", "bash", "write_file", "edit_file"]    # 實作型：要動得了檔案
READONLY_ROLES = {"p01", "p19"}   # 小美（產品經理）、老徐（CTO）


def agent_tools(aid: str) -> list[str]:
    return READONLY_TOOLS if aid in READONLY_ROLES else WRITER_TOOLS


def agent_doc(aid: str, body: str) -> str:
    """把人設寫成 cogito 具名 agent 的格式（frontmatter name/description + body 當 system prompt）。
    description 會出現在主持人的工具說明裡——那不是註解，是 prompt：寫得爛，主持人就不知道
    什麼時候該點名這個人。所以取人設的「決策偏好」那句而不是職稱了事。"""
    p = agents[aid].persona
    desc = "；".join(x for x in [p.get("role", ""), p.get("personality", "")] if x)
    return (f"---\nname: {p.get('name', aid)}\ndescription: {desc}\n"
            f"tools: [{', '.join(agent_tools(aid))}]\n---\n"
            f"{SOUL_MARK} {aid} 由 backend/personas/{aid}.md 產生，刪掉這行即可自行維護。 -->\n\n"
            f"{body.strip()}\n")


def sync_agents() -> int:
    """把六個人設另外投影成 kanban 頻道的【具名 agent】（.claw/agents/<名字>.md），
    讓主持人能用 spawn_subagent 點名真正的人設，而不是在 task_prompt 裡臨時捏一個。

    只寫進 kanban 頻道：基本模式下每位 agent 就是他自己，不需要具名子 agent；只有協作模式的
    主持人需要點名。這個分界不是刻意設計的，是「誰需要誰」自己落出來的。
    沿用 SOUL_MARK 保護：沒有標記的檔案是人寫的，不覆蓋。"""
    if CHANNELS_DIR is None:
        return 0
    persona_dir = Path(__file__).parent / "personas"
    dst_dir = CHANNELS_DIR / f"office_{KANBAN}" / ".claw" / "agents"
    wrote = 0
    for aid, a in npcs().items():
        src = persona_dir / f"{aid}.md"
        if not src.exists():
            continue
        dst = dst_dir / f"{a.name}.md"
        want = agent_doc(aid, src.read_text(encoding="utf-8"))
        if dst.exists():
            old = dst.read_text(encoding="utf-8")
            if SOUL_MARK not in old:   # ⚠ 保護：手寫的一律不動
                print(f"⚠ {dst} 不是由人設產生的（沒有標記），保留原檔不覆蓋")
                continue
            if old == want:
                continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst.write_text(want, encoding="utf-8")
        wrote += 1
    if wrote:
        print(f"具名 agent 同步（{dst_dir}）：寫入 {wrote}")
    return wrote


def sync_souls() -> dict[str, int]:
    """回傳 {寫入, 無異動, 略過}。略過＝那個 AGENTS.md 是人寫的，我們不覆蓋。"""
    n = {"wrote": 0, "same": 0, "skipped": 0}
    if CHANNELS_DIR is None:
        return n
    for aid in agents:
        src = Path(__file__).parent / "personas" / f"{aid}.md"
        if not src.exists():
            continue
        dst = CHANNELS_DIR / f"office_{aid}" / "AGENTS.md"
        want = soul_doc(aid, src.read_text(encoding="utf-8"))
        if dst.exists():
            old = dst.read_text(encoding="utf-8")
            if not old.startswith(SOUL_MARK):   # ⚠ 保護：手寫的一律不動
                n["skipped"] += 1
                print(f"⚠ {dst} 不是由人設產生的（沒有標記），保留原檔不覆蓋")
                continue
            if old == want:
                n["same"] += 1
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(want, encoding="utf-8")
        n["wrote"] += 1
    if any(n.values()):
        print(f"人設同步 AGENTS.md：寫入 {n['wrote']}、已是最新 {n['same']}、略過手寫 {n['skipped']}")
    return n


def save_state() -> None:
    global _dirty
    _dirty = False
    data = {"history": {a: list(cards) for a, cards in history.items()},
            "conv_npc": conv_npc, "pending_approval": pending_approval,
            "approval_src": approval_src}
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
    for cards in history.values():   # 早於 id 欄位的舊卡沒編號：補號
        for t in cards:
            if not t.get("id"):      # 否則前端整批 data-id="undefined" 互搶同一個 DOM 節點，
                _card_seq += 1       # 事件每輪重覆插入（畫面上就是「一次冒出好幾則」）
                t["id"] = _card_seq
    conv_npc.update(data.get("conv_npc", {}))
    pending_approval.update(data.get("pending_approval", {}))
    approval_src.update(data.get("approval_src", {}))
    # 舊 bug 留下的雜項空殼卡：派工那行曾經自己開卡（見 pending_note 的說明），內容只有
    # 那一句「老闆交辦」，而同一句現在掛在真正的任務卡上——留著只是佔位。
    # 條件收得很窄（雜項 + 只有 ≤1 則事件），新版不會再產生這種卡，所以這段等於一次性清理。
    junk = 0
    for aid, cards in history.items():
        keep = [t for t in cards
                if not (t["task"] == "（雜項）" and len(t.get("events", [])) <= 1)]
        junk += len(cards) - len(keep)
        if len(keep) != len(cards):
            history[aid] = deque(keep, maxlen=20)
            last_report[aid] = history[aid][-1] if keep else None
            if last_report[aid] is None:
                last_report.pop(aid, None)
    if junk:
        print(f"清掉 {junk} 張雜項空殼卡（舊版派工留下的）")
    # 孤兒卡：同一個人【最新那張以外】還開著的 working 卡，一定是上一個行程留下的——
    # 委派的映射（sub_active）只活在記憶體裡，重啟後沒有任何東西會再去收它們，永遠轉圈。
    # 最新那張留著不動，下面那段要用它判斷「可能還在跑」。
    orphan = 0
    for aid, cards in history.items():
        for card in list(cards)[:-1]:
            if card["status"] == "working":
                card["status"] = "lost"
                orphan += 1
    if orphan:
        print(f"清掉 {orphan} 張孤兒卡（上個行程沒收完的委派）")
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
    sync_souls()
    sync_agents()   # kanban 頻道的具名 agent（主持人才點得到名）
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


@app.middleware("http")
async def no_store_dev_assets(request, call_next):
    """/unity 與 /shell 一律不給瀏覽器快取。

    StaticFiles 不送 Cache-Control，Chrome 就會用【啟發式快取】（依 Last-Modified 推算
    新鮮期）——iframe 裡的 WebGL.wasm／WebGL.data 因此可能整份不回頭問伺服器就用舊的，
    新舊建置混在一起 → RuntimeError: memory access out of bounds（實測踩過，而且無痕
    模式永遠正常，因為它的快取是空的，最難聯想到快取）。
    這兩個路徑都是【開發中每天重建】的東西，快取省的那點頻寬不值得這種偵錯成本。
    """
    resp = await call_next(request)
    if request.url.path.startswith(("/unity", "/shell")):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp
_ROOT = Path(__file__).parent.parent
_WEBGL = _ROOT / "unity" / "Builds" / "WebGL"
if _WEBGL.exists():
    app.mount("/unity", StaticFiles(directory=_WEBGL, html=True), name="unity")
else:
    print("ℹ 尚無 WebGL build（unity/Builds/WebGL）——/shell 中間畫布會提示先去 Unity 建置")
app.mount("/shell", StaticFiles(directory=_ROOT / "web", html=True), name="shell")
# 名冊頭像（LimeZu 衍生物，不進 git）：跑 tools/make_avatars.py 產生；沒有就用文字頭像頂替。
# 看板那張是【原創】像素圖（沒有對應的角色素材，文字頭像「看」又看不出是什麼）：
# tools/make_kanban_avatar.py。輸出跟著這個目錄一起被 ignore，進 git 的是那支腳本。
_AVATARS = Path(__file__).parent / "avatars"
if _AVATARS.exists():
    app.mount("/avatars", StaticFiles(directory=_AVATARS), name="avatars")
else:
    print("ℹ 尚無名冊頭像——跑 python3 tools/make_avatars.py 產生（外殼先用文字頭像）")

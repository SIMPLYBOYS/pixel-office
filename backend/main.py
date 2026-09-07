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
import shutil
import subprocess
import sys
import uuid
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
        # 新畫面上線＝它什麼都不知道。清掉「送過了」的記憶，讓 sweep 把徽章重掛上去，
        # 否則重整分頁之後所有徽章都不見（去重會擋住重送）。
        emote_now.clear()
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
             "p05": "chair_4", "p12": "chair_5", "p08": "chair_6",
             "p19": "boss_seat"}   # CTO 的位子在老闆房裡（persona 就寫他多半待在那），坐著辦公
BOSS_DOOR = "boss_1"  # 老闆房走道：等 HITL 審批時站這裡（面向老闆桌）
BOARD = "board_1"     # 白板前：規劃類子 agent 站這裡，不佔工位
# 會議室的六個座位。先前是白板前的四個站位——那時的理由是「16×17 已經飽和，沒有空地
# 放得下長桌」，而地圖往西擴之後那個前提不成立了，所以改成真的進會議室坐下。
#
# 【交錯排】a1,b1,a2,b2,a3,b3：兩人開會時一邊一個、面對面，才像在談事情。
# 照 a1,a2,a3 順序填的話會變成三個人擠在同一側、對面空著，看起來像在罰站。
MEET_SPOTS = ["meet_a1", "meet_b1", "meet_a2", "meet_b2", "meet_a3", "meet_b3"]
COOLER = "cooler_1"   # 飲水機：卡住太久的人去接杯水（think 空轉的投影）
# 各工位【旁邊】的站位：委派時主 agent 走過去，面向坐著的同事——
# 「兩個人在同一張桌子旁」是唯一看得出「他們在協作」的畫面語言。
DESK_SIDE = {"p17": "side_1", "p01": "side_2", "p07": "side_3",
             "p05": "side_4", "p12": "side_5", "p08": "side_6", "p19": BOSS_DOOR}
# 閱讀投影：連續讀檔/查資料 → 低頭看書。cogito 的工具名開頭就分得出讀寫，不必列舉全名。
READ_RE = re.compile(r"^(read|grep|glob|list|search|cat|head|tail|find|fetch|browse|web|get_)", re.I)
reading: set[str] = set()   # 正在「看書」的人——同狀態不重發指令（工具事件很密）
SIT_AT = {"p19": "sit_left"}   # 放下書要坐回去的姿勢；其餘工位都是 sit_up（背對鏡頭入座）
# 遞交投影：委派收件成功時，支援者【當場】面向站在自己桌邊的主 agent 把成果遞出去
# （委派起手式就是主 agent 走到支援者桌邊的 DESK_SIDE，人本來就站在那）。方向＝
# 從支援者工位看向那個站位：chair_1-3 的站位在東、chair_4/5 在西、老闆房門在西。
GIFT_TOWARD = {"p05": "gift_left", "p12": "gift_left", "p19": "gift_left"}   # 其餘 gift_right
GIFT_HOLD = 1.3   # 遞交停留秒數（10 幀 8fps 一輪 1.25s，演一輪整）；測試設 0
# 受傷投影：出錯的那一下整身閃紅（LimeZu hurt 列，3 幀）。Unity 端當一次性動作疊在目前姿勢上、
# 自己退掉——橋只負責「哪一下」，不管時間、不還原。方向跟坐姿走：背對鏡頭坐的人就從背後閃紅。
HURT_HOLD = 1.0   # 支援者失敗後、被叫回座位前停留（move_to 會清掉一次性動作）；測試設 0
SUB_RE = re.compile(r"^\[Subagent(?::([^\]]+))?\]\s*")  # cogito 子 agent 事件前綴


def hurt_of(aid: str) -> str:
    """出錯時的受傷方向＝他坐著面向的方向（sit_up → hurt_up、sit_left → hurt_left）。"""
    return "hurt_" + SIT_AT.get(aid, "sit_up").removeprefix("sit_")


def say(aid: str, text: str) -> dict:
    return {"agent_id": aid, "action": "say", "channel": "public", "text": text}


# 相機優先級階梯（對齊 Unity 的 CameraDirector）。每一級都對得上真實事件源——
# 刻意不為了「讓相機有事做」而降門檻：一般工具呼叫、學到記憶【不觸發移動】。
CAM_MANUAL, CAM_DECISION, CAM_MEETING, CAM_FAILURE, CAM_DISPATCH = 1, 2, 3, 4, 5


async def focus(who: list[str], level: int) -> None:
    """請鏡頭推近這幾個人。空清單＝回基態全景。

    這裡只【提議】，接不接受是 Unity 那邊的紀律（冷卻窗、鎖定中不搶鏡）——
    橋不該去追鏡頭現在在哪，那會變成兩份互相要同步的狀態。
    """
    live = [a for a in who if a in agents and a != KANBAN]   # 看板沒有身體，框不到
    await send_cmd({"action": "focus", "agents": live, "level": level})


async def pose(aid: str, action: str) -> None:
    """讓 NPC 擺一個姿勢（move_to 會自動清掉，不必手動還原）。
    只投影「站著不動看不出來」的狀態——等外部回應、長時間沒事做。"""
    if aid == KANBAN:
        return
    await send_cmd({"agent_id": aid, "action": "use", "target": action})


# ── 頭邊的狀態徽章 ────────────────────────────────────────────────────────────
# 跟泡泡分工：泡泡是轉瞬的（2.5-7 秒消失），講「剛剛發生什麼」；徽章持續掛著，
# 講「他【現在】卡在什麼狀態」——掃一眼辦公室就知道誰動不了，泡泡給不了這個。
emote_now: dict[str, str] = {}    # aid -> 目前掛著的徽章（同狀態不重發）
rate_state: dict[str, str] = {}   # aid -> "alert"（額度真被擋）/"warn"（快滿了）/無


# ── 待審的記憶提案 ──────────────────────────────────────────────────────────
# cogito 的 consolidate 工具會把「這次學到什麼」寫成提案，放進頻道工作區的
# .claw/AGENTS.proposed.md，等人 review（聊天端 `apply memory`）才進長期記憶。
# 它【刻意不自動套用】——好處是安全，代價是沒人看就永遠堆著：接這條的當下，
# 四位員工身上已經有 55 條沒有人知道的提案。這正是徽章的形狀：持續、等你處理。
PROPOSED_FILE = ".claw/AGENTS.proposed.md"
memo_pending: dict[str, int] = {}   # aid -> 待審條數（收工與 sweep 時刷新）


def parse_proposed(aid: str) -> list[dict]:
    """這位員工的待審提案，逐條帶編號。讀不到一律回空——沒設 COGITO_CHANNELS 就整條靜默關閉。

    文法跟 cogito 的 parseProposedMemory 對齊：剝掉 HTML 註解後，`## ` 是任務標題、
    每個有內容的 `- ` 是一條（編號從 1 連號、跨標題不重來）、縮排行是附帶欄位。
    刻意不自己發明格式——【編號必須跟他們一致】，因為放行是把 `apply memory <編號>`
    轉給 cogito 執行的。對不上就會放行到錯的那條，那比沒有這個功能糟得多。

    UPDATE/DELETE 前綴＝會動到既有記憶（cogito 的 IsDestructive）。標出來讓人審的時候
    知道這條不是「多記一件事」，而是要改掉或刪掉已經在庫裡的東西。
    """
    if not CHANNELS_DIR:
        return []
    try:
        raw = (CHANNELS_DIR / f"office_{aid}" / PROPOSED_FILE).read_text(encoding="utf-8")
    except OSError:
        return []           # 沒這個檔＝這位員工還沒產生過提案，不是錯誤
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    out: list[dict] = []
    task = ""
    for line in raw.splitlines():
        indented = line[:1] in (" ", "\t")
        b = line.strip()
        if b.startswith("## "):
            task = b[3:].strip()
        elif b.startswith("- ") and b[2:].strip():
            body = b[2:].strip()
            op = next((v.lower() for v in ("UPDATE", "DELETE") if body.startswith(v + " ")), "")
            out.append({"n": len(out) + 1, "task": task, "text": body, "op": op, "meta": []})
        elif indented and b and out:
            out[-1]["meta"].append(b)      # 觸發／舊值／理由那些附帶欄位
    return out


def count_proposed(aid: str) -> int:
    return len(parse_proposed(aid))


def refresh_proposed(aid: str = "") -> None:
    """刷新待審條數。帶 aid 只刷一個（剛收工的那位），空＝全刷（sweep 的保險）。"""
    for a in ([aid] if aid else list(agents)):
        memo_pending[a] = count_proposed(a)


TIMER_STEPS = 8          # 素材是 8 格的餅圖（綠→黃→橘→紅），見 tools/make_emotes.py
APPROVAL_DEFAULT_S = 300  # 沒解析到期限時的假設，與 office_report 的倒數同一個數字


def approval_step(aid: str) -> int | None:
    """審批倒數走到第幾格（0＝剛送來、7＝快逾時了）。沒在等審批就回 None。

    格數【在橋算】而不是讓 Unity 自己倒數：這跟 office_report 的剩餘秒數是同一個
    決定（見那裡的註解）——瀏覽器跟橋的時鐘不一定同步，倒數基準只能有一份。
    兩邊各算一次的話，卡片上寫「剩 30 秒」而頭上的餅還是半滿，誰也不知道該信哪個。
    """
    if aid not in approval_at:
        return None
    total = approval_meta.get(aid, {}).get("timeout_s") or APPROVAL_DEFAULT_S
    frac = (time.time() - approval_at[aid]) / total
    return max(0, min(TIMER_STEPS - 1, int(frac * TIMER_STEPS)))


approval_tick: dict[str, asyncio.Task] = {}   # aid -> 正在推倒數的那個任務


async def tick_approval(aid: str) -> None:
    """審批期間推著倒數徽章往前走。

    一次性任務而不是丟進全域輪詢：格子的邊界時間算得出來，就睡到邊界再更新——
    比每 N 秒醒來看一次準，也不會因為 sweep 週期（30 秒）比格寬（37 秒）接近而跳格。
    審批一收（做了決定／逾時自動拒絕）迴圈條件就不成立，自己結束，不需要另一個清理者。
    """
    if (old := approval_tick.get(aid)) and not old.done():
        return            # 同一個人重複收到審批訊息時，別養出第二個推倒數的任務
    approval_tick[aid] = asyncio.current_task()
    while (step := approval_step(aid)) is not None:
        await sync_emote(aid)
        if step >= TIMER_STEPS - 1:
            await asyncio.sleep(15.0)   # 餅已經滿了，畫面不會再變——等收卡就好
            continue
        total = approval_meta.get(aid, {}).get("timeout_s") or APPROVAL_DEFAULT_S
        nxt = approval_at[aid] + total * (step + 1) / TIMER_STEPS
        # 下限只是防空轉，不該粗到把格子壓掉：期限短的審批（cogito 可以送「1 分鐘內無響應」）
        # 一格才 7.5 秒，訂 1 秒沒事，但訂得再粗一點就會整段跳過。
        await asyncio.sleep(max(0.05, nxt - time.time()))
    approval_tick.pop(aid, None)


def want_emote(aid: str) -> str:
    """這個人【現在】該掛什麼徽章。

    刻意做成【宣告式】而不是四處 set/clear：配對式只要漏掉一個清除點，徽章就永遠
    掛在頭上——那正是最糟的一種假投影（狀態早就過了，畫面還說他卡著）。
    這裡從真實狀態算出答案，漏呼叫最多只是晚一輪 sweep 才更新，不會留下殘影。

    順序＝阻塞程度：等人決定 > 額度被擋 > 額度快滿 > 自己在空轉。
    """
    if aid in pending_approval:
        # 等審批有期限（逾時自動拒絕），所以徽章講的不只是「在等你」，還有「剩多久」——
        # 餅圖填滿＋轉紅同時編碼了進度與急迫。算不出期限才退回靜態的藍問號：
        # 不知道剩多久就別畫一個看起來很確定的倒數。
        step = approval_step(aid)
        return f"timer_{step}" if step is not None else "wait"
    if r := rate_state.get(aid):
        return r            # alert 紅驚嘆號／warn 黃驚嘆號
    if aid in watering:
        return "think"      # 空白思考泡：卡住空轉中（人已經走去飲水機了）
    if memo_pending.get(aid, 0):
        # 黃寶石：他學到的東西還擺在那沒人收。放最後——這件事不急，
        # 壓過「有人在等你決定」或「額度被擋」就是排錯輕重。
        return "idea"
    return ""


async def emote(aid: str, name: str) -> None:
    """掛徽章。空字串＝收起來。只有【變了】才送——工具事件很密。"""
    if aid == KANBAN:       # 看板沒有身體，沒有頭可以掛
        return
    if emote_now.get(aid, "") == name:
        return
    emote_now[aid] = name
    await send_cmd({"agent_id": aid, "action": "emote", "target": name})


async def sync_emote(aid: str) -> None:
    """把徽章對齊真實狀態。冪等，隨便呼叫幾次都行。"""
    await emote(aid, want_emote(aid))


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
    reading.discard(aid)   # 走位＝放下書（Unity 端 move_to 也會清姿勢，兩邊狀態要一致）
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
# 上工中的 agent -> 這一輪【開工】的牆鐘時刻。跟 work_last（心跳）不同：那個一直被刷新，
# 這個從頭到尾不動。in_meeting 要拿它跟板子的 mtime 比，所以必須是牆鐘而非 monotonic。
task_start: dict[str, float] = {}
last_work: dict[str, float] = {}  # agent -> 最後一次有任務事件的時刻（久了就趴著睡）
last_tool: dict[str, float] = {}  # agent -> 最後一次【工具】事件的時刻（只有 think 在跑＝卡住）
watering: set[str] = set()        # 卡住而去飲水機的人（工具事件一回來就叫他回位）
sleeping: set[str] = set()        # 已經趴下的人：別每輪重送 move_to + sleep


def release_work(aid: str) -> None:
    """收工/失聯：釋放主 agent 與其名下所有委派卡（沒回報的委派標 lost，不留永久 working）。"""
    work_last.pop(aid, None)
    task_start.pop(aid, None)
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
            await sync_emote(aid)

    refresh_proposed()      # 提案是 consolidate 工具寫的，任務中就可能多出來
    # 徽章對帳：上面那些轉換點都會即時送，這裡是保險——宣告式的好處就是重算一次
    # 永遠安全，漏掉的轉換點最多晚一輪，不會留下「狀態過了徽章還在」的殘影。
    for aid in list(agents):
        await sync_emote(aid)

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
           "implementer": "p12", "performance": "p05", "correctness": "p17",
           # 市場調查類：小樺（產品 Team 的研究員，輔佐 PM）
           "researcher": "p08", "market-research": "p08", "analyst": "p08"}
SPAWN_RE = re.compile(r"^spawn_subagent(?::(\S+))?")
# background=true 的 spawn 會【立刻】回一句回執，那不是成果：
#   「🌀 已在背景啟動子 agent [老徐]（ID: bg-1）。要等它交件就用 subagent_await…」
# 照收的話卡片當場變「已完成」、報告寫著啟動訊息、人也被放出去自由走動。
# 實測回報：三張卡在同一秒全變已完成，內容都是那句回執。
BG_ACK_RE = re.compile(r"^\s*🌀?\s*已在背景啟動子 agent")
# 真正的收件在 subagent_await 的結果裡，每個子 agent 一行：
#   「背景子 agent bg-1 [老徐]：✅ 已完成」 / 「⚪ 已結束（失敗：…）」 / 「🟢 執行中，尚無結果。」
BG_DONE_RE = re.compile(r"^背景子 agent \S+ \[([^\]]*)\]：([✅⚪🟢])", re.M)
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


def archived_boards() -> list[dict]:
    """封存過的板（board.<時間>.json）。新的在前。

    只列不展開：清單要回答的是「有哪幾塊、什麼時候的、講什麼」，一次把九塊板的欄位
    全算出來是白花的。題目與卡數要讀檔才知道，但那是九次小 json——比讓使用者對著
    一排時間戳猜哪塊是哪塊划算得多。
    """
    base = agent_dir(KANBAN)
    if base is None:
        return []
    out = []
    for f in sorted(base.glob("board.*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.name == "board.json":      # 那是活板，不是封存
            continue
        item = {"file": f.name, "mtime": int(f.stat().st_mtime), "task": "", "n": 0}
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            item["task"] = d.get("task", "")
            item["n"] = len(d.get("tasks") or [])
        except (OSError, ValueError):
            item["task"] = "（讀不動）"
        out.append(item)
    return out


def board_file() -> Path | None:
    """目前這塊板。找不到就是還沒上板。

    接受兩種檔名：`board.json`（守則要求的）與 `board-<主題>.json`（主持人實際的習慣——
    它照主題取名，跟 meeting-*.md / spec-*.md 同一套，而且一個主題一塊板其實更合理）。
    跟模型爭檔名是打不贏的仗，而且爭贏了也沒比較好，所以這裡讓步：多的挑最新那個。

    ⚠ 刻意【不】收 `board.<時間戳>.json`——那是「🗑 收掉」封存的舊板，用點號分隔正好區分。
    收進來的話會變成「封存了還一直顯示」。
    """
    base = agent_dir(KANBAN)
    if base is None:
        return None
    if (f := base / "board.json").exists():
        return f
    live = sorted(base.glob("board-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return live[0] if live else None


def in_meeting(parent: str) -> bool:
    """這是【協作模式的會議階段】嗎——看板在跑、而且【這一輪】還沒上板。

    判準跟任務板面板同一個（沒有板子＝還在開會），兩邊必須一致：畫面上寫著
    「會議進行中」、人卻各自坐回工位，那是兩個投影說了不同的話。

    ⚠「還沒上板」指的是【這一輪】，不是「工作區從來沒有過板子」。主持人照主題取名
    （board-<主題>.json），所以舊板子會一直留在工作區——只看「有沒有板子」的話，
    第二輪起從第一秒就被判成「會議已結束」，沒有人會進會議室。
    實測踩過：工作區裡躺著 board-visitor.json 與 board-westwing-spaces.json，
    整輪都沒開成會，六個人直接各自做事（回報：「幾乎沒有進到會議室開會就自行做任務」）。

    所以比的是【時間】：板子要比這一輪開工還新才算數。work_last 是上工中 agent 的
    心跳，它存在就代表這一輪還在跑。"""
    if parent != KANBAN or KANBAN not in busy:
        return False
    f = board_file()
    if f is None:
        return True                      # 一塊板子都沒有：確實還在開會
    started = task_start.get(KANBAN)
    return started is not None and f.stat().st_mtime < started


async def force_stop(aid: str, how: str) -> None:
    """使用者按了中止 → 投影這邊【一定】收乾淨，不管上游停不停得下來。

    中止是使用者的意思表示，不是一個對上游的請求。先前它被寫成請求：CLI 沒有行程時
    只回一句「沒有進行中的任務」什麼都不做，cogito 連不上時直接回錯——兩種情況下
    卡片都永遠開著、人永遠 busy，唯一的出路是等失聯保險五分鐘或去改 state 檔（實際回報）。

    how＝上游到底發生了什麼（砍掉了／通知了／連不上），原話寫進工作串。收得掉是一回事，
    「有沒有真的叫停」是另一回事，兩者不能混為一談——那正是投影誠實的分界。
    """
    stopped.add(aid)
    log_ev(aid, f"🧑‍💼 老闆中止了任務{how}")
    close_card(aid, "stopped")
    clear_approval(aid)
    rate_state.pop(aid, None)
    await sync_emote(aid)      # 卡收了，頭上的問號／額度警示也要跟著下來
    await adjourn(aid)
    reading.discard(aid)
    release_work(aid)          # busy、委派、失聯計時一起收
    notify("agent", aid, alert="error")
    if aid in agents:
        agents[aid].remember("工作任務被老闆中止了")
    await bubble(aid, "⚠ 中斷")
    if desk := WORK_DESK.get(aid):
        await goto(aid, desk)   # 走位順便清掉看書／掏手機的姿勢


async def tell_cogito_stop(aid: str) -> str:
    """把中止轉給 cogito（best-effort）。回一句「實際發生了什麼」——收卡不等於叫得停。"""
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            # 卡在審批時【先送駁回再送中止】：agent 這時阻塞在等審批，中止指令它根本讀不到，
            # 要等逾時自動拒絕才會醒——使用者眼裡就是「按了中止卻還卡在選擇上」。
            if aid in pending_approval:
                await cl.post(f"{COGITO_HTTP}/task", json={"agent": aid, "text": "reject"},
                              headers=cogito_headers("reject"))
                log_ev(aid, "🧑‍💼 中止前先駁回了待審批的操作")
            r = await cl.post(f"{COGITO_HTTP}/task", json={"agent": aid, "text": "/stop"},
                              headers={"Authorization": f"Bearer {COGITO_HTTP_TOKEN}"})
    except httpx.HTTPError as e:
        return f"（cogito 連不上：{type(e).__name__}——畫面收掉了，但沒叫停它）"
    if r.status_code != 202:
        return f"（cogito 回 {r.status_code}——畫面收掉了，但沒叫停它）"
    return "——已通知 cogito，它會在目前這一步跑完後停下"


def approve_hint(verb: str, err: str) -> str:
    """核准送不出去時，把【後果】跟【出路】講出來。

    只講「連不上」不夠：使用者會以為再按一次就好，但 agent 那邊正阻塞著，逾時後會
    自動拒絕——跟他按的相反。而卡刻意留著（收掉會讓人以為高危操作已經授權），
    所以要順便告訴他怎麼脫身。
    """
    if verb != "approve":
        return err
    return (f"{err}——這個核准【沒有送到】agent 那邊，它會等到逾時自動拒絕。"
            "卡留著不收（收掉會看起來像已經批准了）；要現在結束就按駁回或中止。")


async def tell_cogito_reject(aid: str) -> str:
    """把駁回轉給 cogito（best-effort）。回一句「實際發生了什麼」。"""
    if not COGITO_HTTP:
        return "（未設 COGITO_HTTP——畫面收掉了，agent 那邊會等到逾時自動拒絕）"
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            r = await cl.post(f"{COGITO_HTTP}/task", json={"agent": aid, "text": "reject"},
                              headers=cogito_headers("reject"))
    except httpx.HTTPError as e:
        return f"（cogito 連不上：{type(e).__name__}——agent 那邊會等到逾時自動拒絕，結果一樣）"
    if r.status_code != 202:
        return f"（cogito 回 {r.status_code}——agent 那邊會等到逾時自動拒絕，結果一樣）"
    return ""


async def adjourn(parent: str) -> None:
    """散會：把還站在白板前的人請回自己位子。

    會議中刻意【不】在每個人交件時就送他回位（那樣白板前永遠只有一兩個人，看起來不像
    在開會）。代價是散場要有人喊——就是這裡。

    ⚠ 何時喊：【板子一寫好就散】，不是等整個任務做完。會議結束的時刻是「結論定案」，
    定案之後大家就回位子開工——真實的節奏是這樣，而 in_meeting() 用的也是同一條判準
    （有沒有 board.json）。先前綁在收工，等於畫面上「板子都出來了、人還圍在白板前」，
    兩個投影各說各話。收工時再喊一次是兜底：有些任務只開會、不上板。
    """
    if parent != KANBAN:
        return
    for aid, spot in list(occupied.items()):
        if spot in MEET_SPOTS and (desk := WORK_DESK.get(aid)):
            await goto(aid, desk)
            # 散會＝解除忙碌。交過件的人在會議期間是刻意留在 busy 的（否則生活迴圈會
            # 把他從座位上帶走），所以【解除的責任在這裡】——漏掉的話他回到工位後就
            # 再也不會被生活迴圈碰到，也不會被挑去支援，等於變成一尊雕像。
            busy.discard(aid)
            sub_since.pop(aid, None)
    await focus([], CAM_MEETING)   # 散會＝這段沒事了，鏡頭回基態全景


def meet_spot() -> str:
    """挑一個沒人佔的會議室座位；都滿了就退回白板前（擠一點也比不去好）。

    六個座位對上六個人設，正常不會滿。退路留著是因為「沒位子就不去開會」在畫面上
    看不出來——那個人會留在工位打字，跟沒被派到一模一樣。"""
    taken = set(occupied.values())
    return next((s for s in MEET_SPOTS if s not in taken), BOARD)


def pick_sub_npc(parent: str, name: str) -> str | None:
    free = [x for x in npcs() if x != parent and x not in busy]   # 看板沒有身體，不能被派去支援
    # 先用人名對名冊，再退回角色表。
    # kanban 頻道的具名 agent 用的是【人名】（小美、老徐…），SUB_NPC 那張表收的卻是【角色名】
    # （planner、implementer…）。只查角色表的話「派給小美」會落到隨便一個閒著的人身上——
    # 於是板子寫「👤 小美」、畫面上走過去的是阿海，兩邊各說各話。
    # 本人正忙就仍然退回別人代打：演出可以換角，但不能停演。
    by_name = npc_by_name(name)
    cand = by_name or SUB_NPC.get(name)
    # 兩套命名空間錯位是這個 bug 藏最久的原因：SUB_NPC 收的是【角色名】（implementer…），
    # 但 cogito 的具名 agent 是【人名】的檔案。主持人拿角色名當 agent_type 時，橋這邊照樣
    # 對得到一個 NPC（畫面完全正常），cogito 那邊卻載不到人設——於是「派給專員」變成
    # 「派給沒有人設的探路者」，而且完全看不出來。實測：指名小美與阿哲，實際跑的是小美與小葵。
    # 判準是【有沒有人設】而不是【橋認不認得】。
    if name and by_name is None:
        print(f"⚠ 委派用的 agent_type「{name}」不是員工姓名——cogito 那邊載不到人設，"
              f"實際跑的是沒有人設的探路者（守則要求用人名，如「小美」）")
    if cand in free:
        return cand
    return free[0] if free else None


async def finish_sub(parent: str, name: str, ok: bool, detail: str) -> bool:
    """收掉一張委派卡：主 agent 回位、支援者解除忙碌、關卡、冒泡、記錄。
    找不到對應的人就回 False（沒開成卡，交給呼叫端退回主 agent 冒泡）。

    兩條路徑共用：同步 spawn 的 result，以及 background spawn 之後 subagent_await 的收件。
    先前只有前者，於是背景派工的卡片是靠【啟動回執】關掉的——關錯了時機。
    """
    queue = sub_active.get((parent, name)) or []
    npc = queue.pop(0) if queue else None
    if not queue:
        sub_active.pop((parent, name), None)
    if npc is None:
        return False
    # 交付戲：收件【成功】時，支援者面向站在自己桌邊的主 agent 把成果遞出去——
    # 要在主 agent 動身回位【之前】演，先走人再遞是對著空氣遞。不用走位、不等抵達回報：
    # 人本來就在對的位置上（委派起手式：主 agent 走到 DESK_SIDE、支援者坐自己工位）。
    # 失敗不遞（沒有東西可交）；主持人（kanban）沒有身體，遞給空氣也免了。
    if ok and parent != KANBAN and not in_meeting(parent):
        await pose(npc, GIFT_TOWARD.get(npc, "gift_right"))
        await asyncio.sleep(GIFT_HOLD)
    elif not ok:
        # 失敗沒有東西可交——但也不能什麼都沒發生：支援者閃紅一下，再被叫回座位
        await pose(npc, hurt_of(npc))
        await asyncio.sleep(HURT_HOLD)
    if desk := WORK_DESK.get(parent):   # 交接完主 agent 回自己位子繼續
        await goto(parent, desk)
    # 支援者也要回位子——但【開會中不散會】。
    #
    # 前半是為了「別卡在走道」加的（規劃類被派到白板前，交完件沒人叫他走）。
    # 後半是實測補的：會議中三個人交件時間錯開，一交件就各自回位，白板前永遠只有
    # 一兩個人，看起來完全不像在開會（回報：「沒有明顯站立開會的感覺」）。
    # 一個人講完話不代表會議結束了——散會是整場的事，收在 kanban 收工那裡做。
    #
    # ⚠ busy 也要一起判斷，不能只判斷「送不送回工位」。生活迴圈是
    # `while a.id in busy: sleep()`——一被釋放它就接管，在 idle 間隔內隨機發一個
    # move_to，人就自己從會議室走掉了。實測回報：「有的 agent 會提前自行離開」。
    # 先前只擋了「回工位」那半，等於留在座位上但沒人管，下一個 tick 照樣被帶走。
    if not in_meeting(parent):
        if npc_desk := WORK_DESK.get(npc):
            await goto(npc, npc_desk)
        busy.discard(npc)
        sub_since.pop(npc, None)   # 留著＝watchdog 仍在計時，會議卡死也有兜底
    notify("roster")
    close_card(npc, "ok" if ok else "error")
    card = last_report.get(npc)
    if card:
        card["report"] = detail
    mark = "✔ 回報：" if ok else "✗ 失敗："
    await bubble(npc, "✓ 回報完成" if ok else "⚠ 回報出錯了")
    log_ev(npc, f"{mark}{detail[:200]}")
    agents[npc].remember("完成了委派工作" if ok else "委派的工作失敗了")
    return True


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
            # 開會中就進會議室入座，否則各自回工位。
            # 這一行是「會議室」的全部——投影的差異只有【去哪裡】，因為真實世界的差異也只有這個：
            # 會議階段大家在會議室談，上板之後各自回位子做事。
            # 規劃類的活也在白板前做（不佔工位，讓「這是在想、不是在寫」看得出來）。
            spot = (meet_spot() if in_meeting(parent)
                    else BOARD if name in ("planner", "correctness")
                    else WORK_DESK.get(npc))
            if spot:
                await goto(npc, spot)
            if spot in MEET_SPOTS:   # 開會＝多人聚集，鏡頭帶過去（同級裡多人贏單點）
                await focus([a for a, sp in occupied.items() if sp in MEET_SPOTS], CAM_MEETING)
            elif spot:
                await focus([npc], CAM_DISPATCH)   # 剛派工
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
            # background=true 只是【啟動】成功，不是交件。卡片繼續開著、人繼續忙，
            # 真正的收件在 subagent_await 的結果那裡（見下面 AWAIT 分支）。
            if BG_ACK_RE.match(detail or ""):
                return True
            return await finish_sub(parent, name, kind == "result", detail)
        return True  # spawn 的其他事件不投影

    # subagent_await 的結果＝背景子 agent 的【真正】交件。一次可能收好幾個人，
    # 逐行對人設名收；還在跑的（🟢）不收——它只是這次沒等到，卡片要繼續開著。
    #
    # ⚠ 每張卡只放【自己那一段】。整包塞進去的話，三個人的卡片會寫著一模一樣的內容，
    # 而且開頭是「背景子 agent bg-1 [老徐]：✅ 已完成」這種收件格式——那是給主持人看的
    # 訊息標頭，不是那個人的意見。實測踩過：老徐的卡片上是三人份的原始輸出。
    if label.startswith("subagent_await") and kind == "result":
        done = False
        hits = list(BG_DONE_RE.finditer(detail or ""))
        for i, m in enumerate(hits):
            if m.group(2) == "🟢":
                continue
            # 這一段＝從本行結尾到下一個標頭（或全文結尾）
            end = hits[i + 1].start() if i + 1 < len(hits) else len(detail)
            # 標頭行是「…：✅ 已完成」，成果從【下一行】才開始。失敗那種
            # （「⚪ 已結束（失敗：…）」）整句都在同一行、沒有換行，就整句留著。
            seg = detail[m.end():end]
            own = (seg.split("\n", 1)[1] if "\n" in seg else seg).strip() or m.group(0)
            if await finish_sub(parent, m.group(1), m.group(2) == "✅", own):
                done = True
        return done
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

    兩條路的排版改寫與長度都可能不同，所以比【正規化後的前綴】（sig）而不是整段。
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
    """事件 → 時間軸一行；None＝不記（think/turn）。

    這裡是「dashboard 看得到、辦公室看不到」的主要漏斗（實際回報：對照 run view 才發現
    工作串訊息不完整，常不知道下一步怎麼操作）——所以原則改成：**cogito 已經替每種
    事件截好長度（msg 8000/result 400/tool 120），橋不再二次剪裁**。二次剪裁砍掉的
    正好是 agent 放在訊息尾巴的行動指示（「下一步需要你確認 X」）。
    """
    if kind == "tool":
        if label in WRITE_TOOLS and detail:
            if (block := write_tool_text(label, detail)) is not None:
                return block
        return f"▸ {label}" + (f"｜{detail}" if detail else "")
    if kind == "result":
        # 先前只畫「✓ 工具名」——cogito 明明帶了結果預覽，被整個丟掉；
        # dashboard 有 160 字預覽，工作串 0 字，落差就是這樣來的。
        return f"✓ {label}" + (f"｜{detail}" if detail else "")
    if kind == "error":
        return f"✗ {label}" + (f"：{detail}" if detail else "")
    if kind == "msg":
        return label  # 全文入卡。去重（sig）比正規化前 40 字、卡片事件數有上限，放寬安全
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

    # 板子一寫好就散會——會議結束的時刻是「結論定案」，不是整個任務做完。
    # 沒有「board.json 被建立」這種事件可以掛，所以每則看板事件順手檢查一次：
    # 還有人站在白板前、但板子已經出來了＝該散了。判斷便宜（一次 exists），而且是自癒的
    # ——漏掉一次，下一則事件會補上。
    if aid == KANBAN and not in_meeting(KANBAN):
        await adjourn(KANBAN)

    if aid in work_last or kind == "start":  # 上工中任何事件（含 think/turn）都算心跳
        work_last[aid] = time.monotonic()
    if kind == "start":
        task_start[aid] = time.time()       # 牆鐘：要跟檔案 mtime 比
    last_work[aid] = time.monotonic()  # 有事件＝這位還在做事，重新計算「閒多久」
    sleeping.discard(aid)              # 睡著的被叫醒（下一輪就恢復正常走動）
    # 額度警示：有實質進展就表示不再卡著額度了，收掉紅燈（warn 留到收工——「快滿了」還是真的）
    if kind in ("tool", "result") and rate_state.get(aid) == "alert":
        rate_state.pop(aid, None)
        await sync_emote(aid)

    if kind in ("start", "tool", "result", "error"):   # 有實質進展（think/turn 不算）
        last_tool[aid] = time.monotonic()
        if aid in watering:            # 卡住的人有進展了：回位子繼續
            watering.discard(aid)
            await sync_emote(aid)
            if desk := WORK_DESK.get(aid):
                await goto(aid, desk)

    if kind == "error":
        await focus([aid], CAM_FAILURE)   # 出事了，鏡頭過去

    if await project_sub(aid, kind, label, ev.get("detail", "")):
        return {"ok": True}

    # 審批逾時（自動拒絕）後工作恢復：人還杵在老闆房門口，看到工具事件就自己回工位
    if kind == "tool" and aid not in pending_approval and occupied.get(aid) == BOSS_DOOR:
        if desk := WORK_DESK.get(aid):
            await goto(aid, desk)

    # 出錯投影：整身閃紅一下。先前 error 只有鏡頭推過去，身體毫無反應——出錯是任務裡
    # 最該看得見的一刻。只認主 agent 自己的事件（子 agent 失敗在 finish_sub 那條演）。
    if kind == "error" and aid in busy and not SUB_RE.match(label):
        await pose(aid, hurt_of(aid))

    # 閱讀投影：讀類工具 → 低頭看書；換到非讀類工具（或出錯）→ 放下書坐回去。
    # 只認主 agent 自己的事件（[Subagent:…] 前綴是別人的手，project_sub 沒接走的才會到這）。
    if kind == "tool" and aid in busy and not SUB_RE.match(label):
        if READ_RE.match(label):
            if aid not in reading:
                reading.add(aid)
                await pose(aid, "book")
        elif aid in reading:
            reading.discard(aid)
            await pose(aid, SIT_AT.get(aid, "sit_up"))
    elif kind == "error" and aid in reading:
        reading.discard(aid)
        await pose(aid, SIT_AT.get(aid, "sit_up"))

    chatting = aid in chat_mode  # 這一輪是閒聊：不開卡、不走位、不冒任務泡
    if kind == "start":
        busy.add(aid)
        stopped.discard(aid)   # 新任務＝上一次中止翻篇，別讓它吃掉這張卡的收尾
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
        # 全文入時間軸（原本砍到 200 字）。agent 的行動指示常在訊息【尾巴】——「板子開好了，
        # 下一步需要你回覆 X」——砍掉的正好是那段（實際回報：對照 dashboard 才發現訊息不完整，
        # 常不知道下一步怎麼操作）。card["report"] 只留最後一則，中間輪次的全文只有這裡。
        log_ev(aid, label)
    elif kind == "done":  # 收工：釋放主 agent＋名下委派卡，回歸 idle
        # 使用者剛按過中止：卡已經收好了。這裡再走一次會把報告蓋成「CLI 異常結束
        # （退出碼 -9）」——那個 -9 是【我們自己砍的】，把它寫成異常等於自己騙自己，
        # 而且會再叮一聲、再記一行「任務中斷」，跟上一行自相矛盾。
        if aid in stopped:
            stopped.discard(aid)
            release_work(aid)
            return {"ok": True}
        if chatting and (desk := WORK_DESK.get(aid)):
            await goto(aid, desk)   # 閒聊結束：轉回去繼續坐著（move_to 會清掉轉頭的姿勢）
        chat_mode.discard(aid)
        # cost 是 cogito 算好的【真實】花費（協定：0/未知不送）。只認正數——
        # 沒有數字就什麼都不顯示，寧可空白也不畫 $0.0000 假裝免費。
        cost = ev.get("cost")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
            cost = None
        if not chatting:  # 閒聊不改任務卡狀態（那張卡早就完成了）
            close_card(aid, "ok" if label == "ok" else "error")
            card = last_report.get(aid)
            if card and label != "ok" and ev.get("detail"):
                card["report"] = card["report"] or ev["detail"]
            if card and cost:
                card["cost"] = cost  # **card 攤平：report / history 自動帶到外殼
            # 主 agent 實際跑的模型。沒有它，花費就沒有分母——$0.0231 是 haiku 跑十輪
            # 還是 opus 跑兩輪？看不出來。子 agent 各自的模型不在這裡（見協定說明）。
            if card and (mu := ev.get("model")):
                card["model"] = str(mu)[:60]
            # 這筆花費的【單價】是估的（該模型沒登記定價）。token 是真的、單價是猜的，
            # 差幾倍都可能——不標的話，估計值長得跟實價一模一樣。
            if card and cost and ev.get("cost_est"):
                card["cost_est"] = True
        clear_approval(aid)  # 任務結束，殘留審批卡（逾時自動拒絕）一併收掉
        rate_state.pop(aid, None)
        refresh_proposed(aid)   # 這一刻剛跑完 consolidate 的話，提案就是現在多出來的
        await sync_emote(aid)
        await adjourn(aid)               # 散會：把還站在白板前的人請回位子
        if aid in reading:               # 收工放下書（done 不一定伴隨走位，姿勢要顯式還原）
            reading.discard(aid)
            await pose(aid, SIT_AT.get(aid, "sit_up"))
        release_work(aid)
        if not chatting:
            notify("agent", aid, alert="done" if label == "ok" else "error")
            paid = f"（${cost:.4f}）" if cost else ""  # 中斷也標——燒掉的錢不因失敗就不見
            log_ev(aid, ("✔ 任務完成" if label == "ok" else "✗ 任務中斷") + paid)
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
    left = None
    if aid in approval_at:   # 剩餘秒數在橋算：瀏覽器的時鐘跟橋不一定同步，倒數基準只能有一份
        total = approval_meta.get(aid, {}).get("timeout_s", 300)
        left = max(0, int(total - (time.time() - approval_at[aid])))
    return {"ok": True, "agent": aid, "name": name, **card,
            "approval": pending_approval.get(aid, ""),
            "approval_meta": approval_meta.get(aid),
            "approval_left": left,
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
# 審批用的【第二把】鑰匙。cogito 把派工權與審批權分開：approve/reject 要帶 X-Approver-Token 才以
# 審批身分進去，否則以派工者身分送進去會被 isAdmin 擋下。沒設就等於這個外殼沒有審批權——
# 按核准會得到 cogito 的「🚫 只有管理員可以 approve/reject」，那是真話，不是 bug。
COGITO_HTTP_APPROVER_TOKEN = os.environ.get("COGITO_HTTP_APPROVER_TOKEN", "")


def cogito_headers(text: str = "") -> dict:
    """送 cogito 的標頭。approve/reject 才帶審批鑰匙——派工不帶，兩把鑰匙各開一扇門。"""
    h = {"Authorization": f"Bearer {COGITO_HTTP_TOKEN}"}
    verb = text.strip().split()[0].lower() if text.strip() else ""
    if verb in ("approve", "reject") and COGITO_HTTP_APPROVER_TOKEN:
        h["X-Approver-Token"] = COGITO_HTTP_APPROVER_TOKEN
    return h
APPROVAL_PREFIX = "⚠️ *高危操作審批請求*"                  # chatbot approval.go 的卡片開頭
RESUME_NUDGE_PREFIX = "[系統] 先前因暫時性錯誤"            # core.go resumeNudge：斷點續跑的系統提示
PROGRESS_PREFIXES = ("🤔", "🛠️", "✅ *執行成功*", "⚠️ *執行報錯*")  # OfficeReporter 已投影過，去重
pending_approval: dict[str, str] = {}  # npc id -> 待審批卡文字（shell 顯示核准/駁回按鈕）
# 審批卡是 approval.go 的固定樣板——收卡時就解析成結構化欄位，外殼直接排版面（工具一顆
# chip、參數整段折行、任務 ID 降級成小字、逾時做成倒數），不用靠 markdown 碰運氣。
# 解析不了（樣板改版、舊狀態檔復原的卡）就退回原文渲染——結構化是加分，不是門檻。
APPROVAL_RE = re.compile(r"• 工具: `([^`]+)`\n• 參數: `?(.*?)`?\n任務 ID: `([^`]+)`", re.S)
APPROVAL_TIMEOUT_RE = re.compile(r"(\d+)\s*分鐘內無響應")
approval_meta: dict[str, dict] = {}   # npc id -> {tool, params, task_id, timeout_s}
approval_at: dict[str, float] = {}    # npc id -> 收卡的牆鐘時間（倒數的起點）


def parse_approval(text: str) -> dict | None:
    m = APPROVAL_RE.search(text)
    if not m:
        return None
    t = APPROVAL_TIMEOUT_RE.search(text)
    return {"tool": m.group(1), "params": m.group(2).strip(), "task_id": m.group(3),
            "timeout_s": int(t.group(1)) * 60 if t else 300}


def clear_approval(aid: str) -> None:
    """收審批卡（四份狀態一起收——漏一份就是下一個「卡收了倒數還在跑」的 bug）。"""
    pending_approval.pop(aid, None)
    approval_src.pop(aid, None)
    approval_meta.pop(aid, None)
    approval_at.pop(aid, None)
stopped: set[str] = set()             # 剛被使用者按中止的人——收尾事件到達時別再喊一次「中斷」
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


@app.post("/office/focus")
async def office_focus(ev: dict):
    """老闆手動聚焦：點誰就把鏡頭鎖在誰身上，空的＝解鎖回基態。

    這是優先級階梯的最高級，而且【只有他能解】——正在讀某個人的工作串時，
    鏡頭被別的事件搶走比不動更煩，那叫打斷。鎖與解鎖的紀律在 Unity 的 CameraDirector，
    橋只負責把「老闆現在在看誰」這件事送過去。
    """
    # 看板【是】一個 agent，但沒有身體——focus 會把它濾掉。所以這裡要一起判斷，
    # 否則會回報 locked=true 而鏡頭其實框不到任何人，兩邊說法不一致。
    aid = resolve_npc(ev.get("agent", "") or "")
    who = [aid] if aid and aid != KANBAN else []
    await focus(who, CAM_MANUAL)
    return {"ok": True, "locked": bool(who)}


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
        if meta := parse_approval(text):
            approval_meta[aid] = meta
        approval_at[aid] = time.time()
        # 等審批也算工作中：不標 busy 的話生活 idle 迴圈會把罰站走位蓋掉
        busy.add(aid)
        work_last[aid] = time.monotonic()
        notify("roster", aid, alert="approval")   # 最需要抬頭的一件事：有人在等你決定
        await sync_emote(aid)                    # 頭上掛倒數：站在老闆房門口的人在等【你】
        asyncio.create_task(tick_approval(aid))  # 之後每過一格自己往前推
        await focus([aid], CAM_DECISION)          # 也是鏡頭的最高非手動級：球在老闆手上
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
    log_ev(aid, f"💬 {text}")   # 全文入卡（與 msg 路對齊）；重複由 log_ev 統一擋（同一則訊息會走兩條路送來）
    return {"ok": True}


# 能力清單（工具／技能）：問 cogito 本人，不在橋這邊寫死——工具是按頻道組裝的
# （MCP、背景任務、自我進化都是條件式掛載），寫死的表遲早跟現實不符。
#
# 這是【全員共用】的清單，不是某個人的屬性：工具在各頻道各自 rooted 到自己的工作目錄，
# 但掛上的是同一組；技能更是全 bot 讀同一份 .claw/skills。所以挑任一位員工去問即可。
# 快取到行程結束：同一個 bot 跑著的期間清單不會變。
_caps_cache: dict | None = None
_caps_at: float = 0.0
# 能力會變（cogito 加掛 MCP、換技能）。永久快取的話，改了設定卻要重啟整個橋才看得到
# ——那正是「畫面說的跟事實不同」。五分鐘：夠短到改完泡杯咖啡回來就對了，
# 又不會讓每次開頁都打一次 cogito。
CAPS_TTL = 300.0


def cli_caps_reply() -> dict | None:
    """CLI 回報過的能力（來自最近一次 system/init）。沒跑過就回 None——不假裝知道。

    刻意標 source＝哪個引擎、什麼時候收到的：cogito 與 CLI 的工具集完全不同
    （前者是 read_file/bash/spawn_subagent…，後者是 Read/Edit/Task…），
    混在一起而不說清楚，比沒有更誤導。
    """
    if not cli_caps.get("tools"):
        return None
    name = agents[cli_caps["agent"]].name if cli_caps.get("agent") in agents else cli_caps.get("agent", "")
    return {"ok": True, "tools": cli_caps["tools"], "skills": cli_caps.get("skills") or [],
            "mcp": cli_caps.get("mcp") or [],
            "source": f"Claude Code CLI（{name} 於 {cli_caps.get('at', '')} 回報）"}


@app.get("/office/proposed/{aid}")
def office_proposed(aid: str):
    """這位員工待審的記憶提案。編號跟 cogito 的 `memory list` 一致（見 parse_proposed）。"""
    if aid not in agents:
        return {"ok": False, "error": "沒有這位員工"}
    return {"ok": True, "agent": aid, "name": agents[aid].name,
            "items": parse_proposed(aid),
            # 沒有 cogito 就【放行不了】——先講清楚，別讓人按了才發現。
            "can_apply": bool(COGITO_HTTP)}


@app.post("/office/proposed/{aid}")
async def office_proposed_act(aid: str, d: dict):
    """放行／丟棄提案。實際動作【轉給 cogito 執行】，橋不自己搬檔案。

    為什麼不自己做：放行不只是把那行搬走——cogito 那邊還要寫進 .claw/memory/、
    處理 UPDATE/DELETE 的樂觀鎖比對、記自動放行的撤回窗。在這裡重寫一份，兩邊遲早
    對不上，而對不上的後果是【改錯或刪錯既有記憶】。橋只做它該做的：把人的決定送過去。
    """
    verb = str(d.get("verb") or "")
    if verb not in ("apply", "reject"):
        return {"ok": False, "error": "verb 只能是 apply 或 reject"}
    if aid not in agents:
        return {"ok": False, "error": "沒有這位員工"}
    nums = [int(n) for n in (d.get("nums") or []) if str(n).isdigit()]
    total = len(parse_proposed(aid))
    if bad := [n for n in nums if not 1 <= n <= total]:
        return {"ok": False, "error": f"編號超出範圍（現有 1–{total}）：{bad}"}
    if not COGITO_HTTP:
        return {"ok": False, "error": "未設 COGITO_HTTP——提案存在 cogito 的工作區，"
                                      "只有它能放行。這裡改不了。"}
    cmd = f"{verb} memory" + ("".join(f" {n}" for n in nums) if nums else "")
    try:
        async with httpx.AsyncClient(timeout=10) as cl:
            r = await cl.post(f"{COGITO_HTTP}/task", json={"agent": aid, "text": cmd},
                              headers={"Authorization": f"Bearer {COGITO_HTTP_TOKEN}"})
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"cogito 連不上（{type(e).__name__}）——一條都沒動"}
    if r.status_code != 202:
        return {"ok": False, "error": f"cogito 回 {r.status_code}：{r.text[:120]}——一條都沒動"}
    # cogito 是非同步收下的（202），檔案不會在這一刻就改好。刻意【不】立刻回報新數字：
    # 現在讀到的還是舊的，回一個「還沒變」的數字看起來像沒生效。等下一輪 sweep 對齊。
    log_ev(aid, f"🧑‍💼 老闆{'放行' if verb == 'apply' else '丟棄'}了記憶提案"
                f"（{'第 ' + '、'.join(map(str, nums)) + ' 條' if nums else '全部'}）")
    return {"ok": True, "sent": cmd}


@app.get("/office/caps")
async def office_caps():
    global _caps_cache, _caps_at
    if _caps_cache and time.time() - _caps_at < CAPS_TTL:
        return _caps_cache
    # CLI 模式的能力來自 CLI 自己（init 事件帶的工具/技能/MCP）。cogito 沒開時它就是唯一
    # 的來源——先前一律問 cogito，於是純 CLI 用法下面板只剩「取不到能力清單」，
    # 看起來像「這個模式沒有能力」，實際上它有 184 個工具。
    if not COGITO_HTTP:
        return cli_caps_reply() or {"ok": False, "error": "未設 COGITO_HTTP——問不到 cogito 的能力清單"}
    probe = next(iter(agents), "")   # 清單全員相同，隨便挑一位當探針
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            r = await cl.get(f"{COGITO_HTTP}/capabilities", params={"agent": probe},
                             headers={"Authorization": f"Bearer {COGITO_HTTP_TOKEN}"})
        r.raise_for_status()
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        # cogito 連不上：先給上一份好的（過期也照給——稍舊的清單遠比空白有用），
        # 再退到 CLI 回報的，最後才是一句錯誤。
        return _caps_cache or cli_caps_reply() or {
            "ok": False, "error": f"取不到能力清單：{type(e).__name__}"}
    # mcp：外部 MCP 工具不個別註冊（cogito 只掛 mcp_call_tool／mcp_describe_tool 兩個閘道），
    # 所以清單得從 gateway 的目錄另外拿——否則看板上只看得到兩個閘道，看不出實際掛了什麼。
    _caps_cache = {"ok": True, "tools": d.get("tools") or [], "skills": d.get("skills") or [],
                   "mcp": d.get("mcp") or [], "source": f"cogito（{time.strftime('%H:%M')} 更新）"}
    _caps_at = time.time()
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
INFRA_FILES = {"AGENTS.md", "CLAUDE.md"}   # ＝SOUL_FILES；那個常數定義在後面，這裡寫死避免順序依賴


def infra_file(name: str) -> bool:
    return name in INFRA_FILES


# ── git 鏡頭：工作區列表在 worktree 裡改看「他改了什麼」───────────────────────
# 為什麼需要換鏡頭：工作區原本的權威是 mtime（「這輪剛產出的檔案」標黃），那對員工【產出】
# 的檔案成立——它們本來不存在。但 worktree 裡的檔案本來就在，而且 `git worktree add` 的
# checkout 會把每個檔案的 mtime 蓋成當下（實測：原檔 08/01 → worktree 09/02 11:20）。
# 於是綁完 repo 的十分鐘內，整個專案幾千個檔案全部標黃——訊號在最需要的時刻變成噪音。
#
# 同一棵樹、兩個鏡頭：worktree 外看 mtime（產出了什麼），worktree 內看 git（改了什麼）。
# 後者同時把驗收從「自己開終端機比對」變成一眼可見。
GIT_BASE_KEY = "office.base"   # worktree 的出發點 commit，bind_repo 當下寫進 git config
GIT_STAT_MAX = 3000            # 改動檔案數上限：超過就只給總結，不逐檔標（那多半是誤操作）


def git_out(d: Path, *args: str, timeout: int = 10) -> str | None:
    """在 d 跑一個唯讀 git 指令。失敗（非 repo／git 不在／逾時）一律回 None——
    鏡頭是加值層，壞了就退回 mtime 鏡頭，絕不讓工作區瀏覽整個失敗。

    core.quotePath=false 是必要的：git 預設把非 ASCII 路徑轉義成 "\\350\\210\\207…"，
    於是中文檔名一個都對不上（測試抓到——而 agent 產出的報告幾乎都是中文檔名）。"""
    try:
        r = subprocess.run(["git", "-C", str(d), "-c", "core.quotePath=false", *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def git_lens(d: Path, base: Path) -> dict | None:
    """d 若位於【工作區底下的某個 worktree】內，回這個 worktree 的改動狀態；否則 None。

    比對基準是 bind_repo 記下的出發點（office.base）。沒有它就答不出「他改了什麼」——
    此時只回分支名與一句說明，不猜（猜錯的 diff 比沒有 diff 更糟）。
    """
    top = git_out(d, "rev-parse", "--show-toplevel")
    if not top:
        return None
    root = Path(top.strip())
    # 必須是【工作區底下的】repo。工作區本身若剛好是個 repo（或員工自己 git init 了根目錄），
    # 那不是我們掛的 worktree，不套這個鏡頭。
    if root == base.resolve() or base.resolve() not in root.parents:
        return None
    lens: dict = {"repo": root.name, "_root": str(root),
                  "branch": (git_out(root, "branch", "--show-current") or "").strip(),
                  "changed": {}}
    sha = (git_out(root, "config", "--get", GIT_BASE_KEY) or "").strip()
    if not sha:
        lens["note"] = "找不到出發點（這個 worktree 建立於此功能之前），只能顯示分支"
        return lens
    lens["base"] = sha[:8]
    lens["commits"] = len((git_out(root, "rev-list", f"{sha}..HEAD") or "").split())
    # 已提交＋未提交一起算：員工被要求 commit 到分支上，但收工當下也可能還有沒 commit 的。
    # 兩者對老闆是同一件事——「跟我給他的版本比，現在差在哪」。
    names = git_out(root, "diff", "--name-status", sha) or ""
    untracked = git_out(root, "ls-files", "--others", "--exclude-standard") or ""
    changed: dict[str, str] = {}
    for line in names.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changed[parts[-1]] = parts[0][:1]   # M/A/D/R…
    for line in untracked.splitlines():
        if line.strip():
            changed[line.strip()] = "?"
    lens["total"] = len(changed)
    if len(changed) <= GIT_STAT_MAX:
        lens["changed"] = changed
    if stat := (git_out(root, "diff", "--shortstat", sha) or "").strip():
        lens["stat"] = stat   # 如「3 files changed, 120 insertions(+), 8 deletions(-)」
    return lens


def apply_lens(r: dict, lens: dict, base: Path) -> None:
    """把 git 狀態標到列表的每一筆上。檔案標自己的狀態；資料夾標底下有幾個檔改過
    ——不然「改動藏在三層目錄裡」這件事在列表上完全看不出來，只能一層層點進去找。

    列表項的 path 是相對【工作區根】（listing 的 base），git 的路徑是相對 worktree 根，
    兩者差一段前綴，所以要換算過去。"""
    changed = lens.get("changed") or {}
    root = lens.get("_root")
    if not changed or not root:
        return
    top = Path(root)
    for e in r.get("entries", []):
        try:
            rel = str((base / e["path"]).resolve().relative_to(top))
        except ValueError:
            continue   # 不在這個 worktree 底下（同層還有工作區自己的產出檔）
        if e.get("dir"):
            if n := sum(1 for p in changed if p.startswith(rel + "/")):
                e["git_n"] = n
        elif st := changed.get(rel):
            e["git"] = st


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
        # 改動時間：agent 是【邊做邊寫檔】的，哪些是這次任務剛產出的、哪些是上週留下來的，
        # 光看檔名分不出來。送秒級 epoch 讓前端自己決定要顯示絕對時間還是「3 分鐘前」——
        # 格式化交給看得到使用者時區的那一端做。
        #
        # 只給 mtime 不給 ctime：POSIX 的 st_ctime 是 inode 變更時間（改權限也會動），
        # 不是「建立時間」；真正的建立時間 st_birthtime 只有部分平台有。與其送一個
        # 在 Linux 上會默默變成別的意思的欄位，不如只送一個到處都對的。
        st = f.stat()
        if f.is_dir():
            dirs.append({"name": f.name, "path": r, "dir": True, "mtime": int(st.st_mtime)})
        else:
            ext = f.suffix.lstrip(".").lower()
            files.append({"name": f.name, "path": r, "dir": False, "ext": ext,
                          "size": st.st_size, "mtime": int(st.st_mtime),
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
        # git 鏡頭：這一層若在掛進來的 worktree 內，改用「他改了什麼」而不是 mtime
        if (d := resolve_in(base, p)) and (lens := git_lens(d, base)):
            apply_lens(r, lens, base)
            lens.pop("changed", None)   # 逐檔狀態已經標在列表上了，不必再送一份
            lens.pop("_root", None)     # 內部欄位：絕對路徑不外送
            r["git"] = lens
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


def meeting_progress() -> dict | None:
    """會議進度：這一輪派了誰、回來幾個。沒有在開會就回 None。

    為什麼要這一段：上板【之前】的會議是整個流程裡最久也最貴的一段，而那段時間任務板
    是空的——使用者只能看著工作串一直捲，不知道還要等多久、誰還沒回。

    資料全部來自已經存在的委派紀錄（誰被派、子卡收了沒），不是另外編一個進度條——
    空板子是雜訊，假進度更糟。
    """
    card = last_report.get(KANBAN)
    if not card or card["status"] != "working":
        return None
    people = []
    for e in card["events"]:
        ref = e.get("sub")
        if not ref:
            continue
        child = find_card(ref["agent"], ref["id"])
        people.append({
            "name": agents[ref["agent"]].name if ref["agent"] in agents else ref["agent"],
            "done": bool(child) and child["status"] != "working",
            "ok": bool(child) and child["status"] == "ok",
        })
    if not people:
        return None            # 還沒派任何人＝主持人還在讀資料，沒有進度可報
    return {"ok": True, "mode": "meeting", "task": card["task"], "live": KANBAN in busy,
            "people": people, "done": sum(1 for p in people if p["done"]), "total": len(people)}


@app.get("/office/board")
def office_board(f: str = ""):
    """看板快照。還沒上板時退回【會議進度】；連會都還沒開就整個不顯示。

    f=<board.時間.json> 可以指定看某一塊【封存板】。回應一律附上封存清單，讓外殼
    在「目前沒有活板」時也開得了門——不然收掉最後一塊之後，整個面板連同封存入口
    一起消失，那些紀錄等於被鎖在磁碟上。

    ⚠ 會議進度【不需要工作區】——它的資料全來自任務卡。先前這裡在 agent_dir 為 None 時
    就提早回傳，等於「還沒建過工作區就永遠看不到進度」，順序放錯了。
    """
    arch = archived_boards()
    if f:
        # 只認清單裡的檔名。不接受路徑、不做拼接——這是照使用者輸入去讀磁碟的入口，
        # 白名單比任何字串檢查都可靠。
        if not any(a["file"] == f for a in arch):
            return {"ok": False, "error": "找不到這塊封存板", "archives": arch}
        picked = agent_dir(KANBAN) / f
        return board_snapshot(picked, arch, archived=f)
    live = board_file()
    if live is None:
        r = meeting_progress() or {"ok": False, "error": "還沒有進行中的協作任務"}
        r["archives"] = arch
        return r
    return board_snapshot(live, arch)


def board_snapshot(f: Path, arch: list[dict], archived: str = "") -> dict:
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"{f.name} 讀不動：{type(e).__name__}"}
    tasks = data.get("tasks") or []
    done_ids = {t.get("id") for t in tasks if t.get("status") == "done"}
    cols = {k: [] for k, _ in BOARD_COLUMNS}
    for t in tasks:
        col = board_column(t, done_ids)
        # owner 兩種寫法都吃：守則要求寫【人名】，但早期的板子存的是 persona id。
        # 一律回「看得懂的名字」＋「可跳轉的 id」——前端要靠 owner_id 才點得進那個人的工作串，
        # 只有名字的話還得在前端反查一次名冊，那是把同一個對照表寫兩份。
        owner = t.get("owner")
        owner_id = owner if owner in agents else npc_by_name(owner) if owner else None
        cols[col].append({
            "id": t.get("id", ""), "title": t.get("title") or t.get("id", ""),
            "deps": t.get("deps") or [],
            "owner": agents[owner_id].name if owner_id else owner,
            "owner_id": owner_id,
            "out": t.get("out"),
        })
    # live＝主持人現在真的在推進這塊板。board.json 只有主持人在跑時才會更新，它一收工
    # 「進行中」那欄就永遠停在原地——畫面上看起來有人在做，實際沒有。投影不能說謊：
    # 板子是不是活的，橋知道，就要講出來。
    # 封存板【永遠】不是活的：它是一份紀錄，不是現在的狀態。就算主持人此刻正在跑，
    # 這塊板上的「進行中」也早就停在收掉的那一刻——標成 live 就是說謊。
    return {"ok": True, "task": data.get("task", ""), "live": (not archived) and KANBAN in busy,
            "archived": archived, "archives": arch,
            "columns": [{"key": k, "name": n, "cards": cols[k]} for k, n in BOARD_COLUMNS]}


@app.delete("/office/board")
def office_board_clear():
    """收掉目前這塊板。改名成 board.<時間>.json 而不是刪除——那是一次協作的完整紀錄，
    主持人也可能還想回頭看；真的不要了再自己去工作區刪。"""
    f = board_file()
    if f is None:
        return {"ok": False, "error": "目前沒有板子"}
    # 封存名一律用點號分隔（board.<時間>.json）——board_file() 靠這個把封存檔排除在外，
    # 不然收掉之後它又會被當成「目前這塊板」撿回來。
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


# 協作人數上限：外殼只送一個數字，怎麼講給主持人聽由橋決定——措辭跟 personas/kanban.md
# 的「開會的規矩」是一份合約的兩半，散在前端會慢慢對不上。
#
# 用【附加一行】而不是改 persona：人數是這一次任務的參數，不是這個角色的長期設定。
# 寫進 persona 就變成每一次都適用，下一題還得記得改回來。
def with_headcount(text: str, people: int) -> str:
    if people == 1:
        return (f"{text}\n\n【協作限制】這次只找 1 位成員（不含你這位主持人）——"
                "不必開會，直接請他給意見即可。spec.md、board.json、板子寫好停一次問老闆，"
                "這些一律照舊。")
    return (f"{text}\n\n【協作限制】這次最多找 {people} 位成員參與（不含你這位主持人）。"
            "人選由你判斷，但總數不得超過這個數。")


# ── 任務綁定真實 repo（Devin 對照筆記 ①）───────────────────────────────
# 員工平常在頻道工作區（沙箱）幹活——那不是你的專案。這裡把「這個任務在 repo X 上做」
# 做成派工的一等參數：真實 repo 以 git worktree 掛進員工的工作區。
REPOS_DIR = os.environ.get("OFFICE_REPOS_DIR", "")


def repo_touched(p: Path) -> int:
    """這個 repo 最近一次「有事發生」的時間。

    取 .git 的目錄 mtime：commit／add／checkout／fetch 都會改寫裡面的 index 或 refs
    （git 一律走臨時檔 + rename，所以目錄本身的 mtime 會動），一次 stat 就問得到。
    量過幾個真實 repo：它總是 ≥ 最後一次 commit 的時間，而且反映得到「拉了但沒 commit」。
    跑 `git log` 才準，但 288 個 repo 等於 288 次 subprocess——不值得。
    """
    try:
        return int(max(p.stat().st_mtime, (p / ".git").stat().st_mtime))
    except OSError:
        return 0


def local_repos() -> list[dict]:
    """OFFICE_REPOS_DIR 底下的 git 專案。沒設＝功能不存在（入口資料驅動，外殼不畫欄位）。"""
    if not REPOS_DIR:
        return []
    root = Path(REPOS_DIR).expanduser()
    if not root.is_dir():
        return []
    return [{"name": p.name, "path": str(p), "mtime": repo_touched(p)}
            for p in sorted(root.iterdir()) if (p / ".git").exists()]


def worktree_path(aid: str, name: str) -> Path | None:
    """這位員工掛這個 repo 的 worktree 落點。bind_repo 與「進行中」判斷共用，避免兩邊漂掉。"""
    return None if CHANNELS_DIR is None else CHANNELS_DIR / f"office_{aid}" / name


@app.get("/office/repos")
def office_repos(agent: str = "", sort: str = "recent"):
    """可派工的 repo 清單。帶 agent 時，這位員工【已經在上面工作過的】（worktree 還在）排最前面。

    「這位員工在做的」不記在瀏覽器：磁碟上的 worktree 就是真實狀態——換瀏覽器、清快取、
    重灌都還在，而且天生 per-員工（老徐養文件的 repo 跟小葵改前端的本來就不同）。

    sort：`recent`（預設，最近動過的在前）或 `name`。兩百多個 repo 時，字母序等於要你
    先知道名字才找得到；預設用時間，是因為多數時候你要派的就是手上正在動的那個。
    【進行中的一律置頂】，那是另一個維度（相關性），不受排序選擇影響。
    """
    repos = local_repos()
    order = (lambda r: r["name"].lower()) if sort == "name" else (lambda r: -r["mtime"])
    if agent in agents:
        for r in repos:
            wt = worktree_path(agent, r["name"])
            r["bound"] = bool(wt and wt.exists())
        repos.sort(key=lambda r: (not r["bound"], order(r)))
    else:
        repos.sort(key=order)
    return {"ok": True, "root": REPOS_DIR, "repos": repos, "sort": sort}


def bind_repo(aid: str, repo: dict) -> tuple[str, str] | str:
    """把真實 repo 以 git worktree 掛進員工的頻道工作區。回 (目錄名, 分支)；str＝錯誤訊息。

    為什麼是 worktree 而不是 clone：worktree 與原 repo 共用物件庫，員工 commit 完，
    分支【立刻】出現在你的 repo 裡——驗收合併不必 fetch。也刻意不讓員工直接進原目錄：
    cogito 的檔案工具 rooted 在頻道工作區（越界是工具層硬擋），這條防線不拆。
    已掛過就沿用同一個 worktree／分支——同一位員工對同一個 repo 的工作是連續的。
    """
    dst = worktree_path(aid, repo["name"])
    if dst is None:
        return "未設 COGITO_CHANNELS，找不到員工的頻道工作區"
    if dst.exists():
        r = subprocess.run(["git", "-C", str(dst), "branch", "--show-current"],
                           capture_output=True, text=True, timeout=10)
        return repo["name"], (r.stdout.strip() or "（沿用既有 worktree）")
    dst.parent.mkdir(parents=True, exist_ok=True)
    branch = f"office/{aid}-{time.strftime('%m%d-%H%M')}"
    r = subprocess.run(["git", "-C", repo["path"], "worktree", "add", "-b", branch, str(dst)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return f"worktree 開不出來：{(r.stderr or r.stdout).strip()[-120:]}"
    # 記下【出發點】：驗收時要問的是「他改了什麼」，那需要一個基準 commit。
    # 事後推不出來——分支名不帶來源、reflog 會過期、預設分支名各家不同。當下寫死最可靠，
    # 存在 worktree 自己的 git config 裡：跟著 worktree 走，刪掉 worktree 就一起消失。
    if (sha := subprocess.run(["git", "-C", str(dst), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)).returncode == 0:
        subprocess.run(["git", "-C", str(dst), "config", GIT_BASE_KEY, sha.stdout.strip()],
                       capture_output=True, text=True, timeout=10)
    return repo["name"], branch


def with_repo(text: str, name: str, src: str, branch: str) -> str:
    """跟 with_headcount 同一個道理：repo 是這一次任務的參數，用附加一行講給 agent 聽。"""
    return (f"{text}\n\n【工作 repo】./{name}/ ——這是真實專案 {src} 的 git worktree"
            f"（分支 {branch}，與原 repo 共用歷史）。所有讀寫都在 ./{name}/ 底下進行；"
            "改完 commit 到這個分支即可，【不要 push、不要碰原目錄】——老闆會自己驗收合併。")


# ── CLI 模式（munder-difflin 的核心賣點：用你已經在付的訂閱，而不是按次計費的 API）──
#
# 【為什麼可行】cogito 與 Claude Code CLI 產生的是同一種東西——一串「思考／用工具／
# 拿到結果／回報／收工」的事件。辦公室要的就只有這串事件。所以 CLI 模式不是另一套系統，
# 是【另一個 office 事件的產生者】：走位、泡泡、工作串、報告卡、git 鏡頭全部零改動。
#
# 【實測確認過的形狀】claude -p --output-format stream-json --verbose 吐 NDJSON：
#   system/init          → 開場（模型、工具數、cwd）
#   assistant.tool_use   → 要用某個工具
#   user.tool_result     → 那個工具的結果（is_error 標成敗）
#   assistant.text       → 對人說的話
#   result               → 收工（is_error / num_turns / total_cost_usd）
#   rate_limit_event     → 訂閱額度的訊號（munder 說的「on their hourly limits」）
#
# 【刻意不用 --bare】它會讓 CLI 變成未登入（實測回 "Not logged in · Please run /login"）。
CLI_CMD = os.environ.get("OFFICE_CLI_CMD", "claude")
# 權限模式：-p 模式沒有人可以回答提問，所以必須先講好。預設 acceptEdits（可改檔、
# 但高風險操作會被拒而不是卡住）；要完全放手自己設 bypassPermissions——那等於把這台機器
# 交給 agent，與 cogito 跑 host 模式 bash 是同一級的信任決定。
CLI_PERMISSION = os.environ.get("OFFICE_CLI_PERMISSION", "acceptEdits")
CLI_TIMEOUT = float(os.environ.get("OFFICE_CLI_TIMEOUT", "1800"))  # 這麼久沒收工就砍掉

cli_procs: dict[str, asyncio.subprocess.Process] = {}   # aid -> 執行中的 CLI（供中止）
cli_model: dict[str, str] = {}   # aid -> CLI 上次實際跑的模型（system/init 會報，供介面揭露）
cli_caps: dict = {}              # CLI 上次回報的能力（工具/技能/MCP）——init 全都帶了


def cli_available() -> bool:
    """CLI 找得到才在介面上給這個選項——入口資料驅動，跟 repo 那排同一個原則。"""
    return shutil.which(CLI_CMD) is not None


def cli_events(d: dict, tool_names: dict[str, str]) -> list[dict]:
    """一筆 CLI 事件 → 零到多筆 office 事件。tool_names 記 tool_use_id → 工具名，
    因為結果事件只帶 id，而辦公室要顯示「哪個工具回來了」。"""
    out: list[dict] = []
    t = d.get("type")
    if t == "assistant":
        for c in (d.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and str(c.get("text", "")).strip():
                out.append({"kind": "msg", "label": c["text"]})
            elif c.get("type") == "thinking":
                out.append({"kind": "think", "label": ""})
            elif c.get("type") == "tool_use":
                name = str(c.get("name", "tool"))
                tool_names[str(c.get("id", ""))] = name
                out.append({"kind": "tool", "label": name,
                            "detail": json.dumps(c.get("input"), ensure_ascii=False)})
    elif t == "user":
        for c in (d.get("message") or {}).get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            body = c.get("content")
            if isinstance(body, list):   # 內容可能分段（文字＋圖片）
                body = " ".join(str(x.get("text", "")) for x in body if isinstance(x, dict))
            out.append({"kind": "error" if c.get("is_error") else "result",
                        "label": tool_names.get(str(c.get("tool_use_id", "")), "tool"),
                        "detail": str(body or "")[:400]})
    elif t == "rate_limit_event":
        if line := rate_limit_line(d.get("rate_limit_info") or {}):
            out.append({"kind": "msg", "label": line})
    return out


# 額度警戒線：低於這個用量不吵。90% 是「還能做完手上這件、但該知道了」的位置。
RATE_WARN = 0.9


def rate_limit_line(info: dict) -> str:
    """額度事件 → 要不要講、講什麼。不該講就回空字串。

    【踩過的坑】這個事件是【例行回報】，每次呼叫都可能來一筆，實測內容是
    status=allowed、utilization=0.05——我原本把每一筆都翻譯成「觸到上限」，
    等於在畫面上說謊，而且是那種看起來很像真的的謊。

    真正值得講的只有兩種：真的被擋下來（status 不是 allowed），以及快用完了
    （用量過警戒線）。其餘一律安靜——投影誠實不只是「不要假裝成功」，
    也包括「不要假裝有事發生」。
    """
    if not isinstance(info, dict):
        return ""
    when = ""
    if ts := info.get("resetsAt"):
        try:
            when = f"，約 {time.strftime('%H:%M', time.localtime(float(ts)))} 重置"
        except (TypeError, ValueError, OSError):
            when = ""
    kind = {"five_hour": "5 小時", "seven_day": "7 日"}.get(str(info.get("rateLimitType")), "")
    if str(info.get("status", "allowed")).lower() != "allowed":
        return f"⏳ 訂閱額度已達上限（{kind or '額度'}窗）{when}——等額度恢復中"
    # 還能用，但快滿了才提醒。取兩個窗裡用得最兇的那個。
    worst_name, worst = "", 0.0
    for name, w in (info.get("unifiedWindows") or {}).items():
        try:
            u = float((w or {}).get("utilization", 0))
        except (TypeError, ValueError):
            continue
        if u > worst:
            worst_name, worst = name, u
    if worst >= RATE_WARN:
        label = {"five_hour": "5 小時", "seven_day": "7 日"}.get(worst_name, worst_name)
        return f"⚠ 訂閱額度已用 {worst:.0%}（{label}窗）{when}"
    return ""


def cli_session_id(aid: str, cwd: Path) -> str:
    """這位員工在這個工作目錄的【固定】session id。

    為什麼要固定：CLI 每次派工都是一個新行程。沒有 --session-id 就等於每次都失憶——
    使用者按了中止再下「繼續」，它根本不知道要繼續什麼，只好自己鑽研那兩個字（實際回報）。
    cogito 那條本來就是一個頻道一條 session（磁碟上 office_p17 累積了 44 則、跨多個任務），
    兩個引擎在同一個介面下行為不同、而使用者看不出來，比單純沒有記憶更糟。

    【綁 cwd】是因為 Claude Code 的 session 是按專案目錄收納的
    （~/.claude/projects/<cwd 編碼>/<id>.jsonl，實地確認過：office-p01 與
    office-p01-shop-coupon 各自一個目錄）。所以連續性的粒度是「同一個人 × 同一個工作
    目錄」——換工作 repo 會另起一條。這跟 cogito 的「一個頻道一條」有出入，但那是工具
    的收納方式決定的，硬要跨目錄共用只會在 resume 時找不到。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pixffice/cli/{aid}/{cwd}"))


# Claude Code 收納 session 的地方（<這裡>/<cwd 編碼>/<id>.jsonl）。認 CLAUDE_CONFIG_DIR：
# 搬過 config 的人如果找不到檔案，每次派工都會被判成「沒有舊對話」→ 永遠重開一條。
CLI_SESSION_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"


def cli_session_args(aid: str, cwd: Path) -> list[str]:
    """接回這位員工在這個目錄的對話：沒有就用固定 id 開一條，有就續上去。

    【實測，不是猜的】--session-id 只負責【建立】。對已經存在的 id 再用一次會直接死：
        Error: Session ID <id> is already in use.（退出碼 1）
    所以兩種情況要用不同旗標，靠磁碟上有沒有那個 session 檔來判斷。

    用 glob 搜 id 而不是自己拼目錄名：Claude Code 把 cwd 編碼成目錄
    （/ 和 _ 都變成 -，非 ASCII 也是），這個規則沒有文件、拼錯就等於每次都重開一條
    ——那正是這次要修的病。id 本身已經含了 cwd、全域唯一，直接搜它最穩；
    session 檔被清掉時也會自動退回「開一條新的」，不會卡在 resume 失敗。
    """
    sid = cli_session_id(aid, cwd)
    known = next(CLI_SESSION_DIR.glob(f"*/{sid}.jsonl"), None)
    return ["--resume", sid] if known else ["--session-id", sid]


WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}   # 被擋＝交付物很可能沒落地


def cli_done_events(d: dict) -> list[dict]:
    """CLI 的 result → 收工事件。is_error 只說 CLI 行程有沒有炸，不說任務有沒有交付。

    實測（卡 233）：-p 模式下 Write 被 ask 規則擋掉，CLI 仍以 is_error=false、subtype=success 收工，
    橋照抄就把一張檔案根本不存在的卡標成「✔ 任務完成」。result 其實帶 permission_denials
    （[{tool_name, tool_use_id, tool_input}]），只是先前沒人看。規則：
    - 檔案類工具被擋 → 交付物大概沒寫出去 → label=error，工作串多一行 ⛔ 明講被擋了什麼
    - 其他工具被擋（Bash/WebFetch）→ CLI 多半自己繞過（卡 234 把複合 Bash 拆三條重來）→
      維持 CLI 的判斷，但工作串留一行 ⚠，讓人看得到有東西被擋
    寧可把一張其實成功的卡標成有疑慮，也不要把一張沒交付的卡標成完成。
    """
    denials = [str(x.get("tool_name") or "?") for x in (d.get("permission_denials") or []) if isinstance(x, dict)]
    blocked_write = [t for t in denials if t in WRITE_TOOLS]
    label = "error" if (d.get("is_error") or blocked_write) else "ok"
    out: list[dict] = []
    if denials:
        counts = "、".join(f"{t}×{denials.count(t)}" for t in dict.fromkeys(denials))
        out.append({"kind": "error", "label": (f"⛔ 交付被權限擋下：{counts}——檔案沒寫出去" if blocked_write
                                               else f"⚠ {len(denials)} 個操作被權限擋下：{counts}")})
    out.append({"kind": "done", "label": label, "detail": str(d.get("result") or "")[:120]})
    return out


async def run_cli_task(aid: str, text: str, cwd: Path | None = None, model: str = "") -> None:
    """在員工的工作區跑 CLI，把它的事件流轉成 office 事件。

    刻意重用 office_event 而不是自己改狀態：投影只能有一條路徑，兩條遲早會漂。
    人設也是免費的——CLAUDE.md 已經同步在那個工作區，Claude Code 會讀（它不讀 AGENTS.md，實測過）。

    cwd：綁了工作 repo 時直接【進到 worktree 裡】跑，而不是待在頻道工作區靠一句話
    叫它 cd 進去。理由是 Claude Code 會【自己判斷處境】——往上找 CLAUDE.md/AGENTS.md、
    用 git rev-parse 找 repo 根。頻道工作區在 cogito 的 workspace 底下（那本身是個
    git repo、還有自己的 AGENTS.md），所以待在那裡的 CLI 會合理地認定「我在 cogito-agent」
    ——實際回報過。把它放進 worktree，那些訊號就全部指向正確的專案。
    """
    base = cwd or agent_dir(aid) or (CHANNELS_DIR / f"office_{aid}" if CHANNELS_DIR else None)
    if base is None:
        await office_event({"v": 1, "agent": aid, "kind": "start", "label": text[:80]})
        await office_event({"v": 1, "agent": aid, "kind": "done", "label": "error",
                            "detail": "未設 COGITO_CHANNELS，沒有工作區可跑"})
        return
    base.mkdir(parents=True, exist_ok=True)
    argv = [CLI_CMD, "-p", text, "--output-format", "stream-json", "--verbose",
            "--permission-mode", CLI_PERMISSION,
            *cli_session_args(aid, base)]   # 接回上一次的對話，「繼續」才有東西可繼續
    # --model 吃完整 id（claude-opus-5）或別名（opus）。沒指定就用 CLI 自己的設定——
    # 那是它的預設，不是我們該替它決定的事。
    if model:
        argv += ["--model", model]
    await office_event({"v": 1, "agent": aid, "kind": "start",
                        "label": text[:80], "detail": str(base)})
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(base), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=1 << 22)   # 單行可能很長（工具參數）
    except OSError as e:
        await office_event({"v": 1, "agent": aid, "kind": "done", "label": "error",
                            "detail": f"起不了 CLI（{CLI_CMD}）：{e}"})
        return
    cli_procs[aid] = proc
    tool_names: dict[str, str] = {}
    warned: set[str] = set()
    done_sent = False
    try:
        async def pump() -> None:
            nonlocal done_sent
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "system" and d.get("subtype") == "init":
                    # CLI 用它【自己的】設定選模型，我們無從指定——但可以【講出來】。
                    # 先前的做法是把模型那排整個藏掉，結果看起來像功能不見了（實際回報）。
                    if m := str(d.get("model") or ""):
                        cli_model[aid] = m
                    # init 連工具/技能/MCP 都帶了。先前整包丟掉，於是 CLI 模式下
                    # 能力面板只剩「取不到能力清單」——那不是沒有能力，是我沒收下來。
                    if tools := [t for t in (d.get("tools") or []) if isinstance(t, str)]:
                        globals()["_dirty"] = True   # 讓存檔器把它寫下來（跨重啟要留著）
                        cli_caps.update({
                            "at": time.strftime("%H:%M"), "agent": aid,
                            "tools": [{"name": t, "description": ""} for t in tools],
                            "skills": [{"name": str(x.get("name") or x), "description": str(x.get("description") or "")}
                                       if isinstance(x, dict) else {"name": str(x), "description": ""}
                                       for x in (d.get("skills") or [])],
                            "mcp": [{"name": str(m2.get("name") or ""),
                                     "description": f"狀態：{m2.get('status') or '未知'}"}
                                    for m2 in (d.get("mcp_servers") or []) if isinstance(m2, dict)],
                        })
                    continue
                if d.get("type") == "result":
                    done_sent = True
                    # total_cost_usd 是「換算成 API 會是多少錢」，訂閱制並不會這樣扣。
                    # 標成花費就是說謊，所以不送 cost——額度用量另外講（見 msg）。
                    for ev in cli_done_events(d):   # 被權限擋下的交付不算完成（見函式說明）
                        await office_event({"v": 1, "agent": aid, **ev})
                    continue
                for ev in cli_events(d, tool_names):
                    # 額度提醒每次 API 呼叫都會來一筆，同一句話講一次就夠
                    if ev["kind"] == "msg" and ev["label"].startswith(("⏳", "⚠")):
                        # 頭上掛額度警示。這是目前【唯一】完全沒有身體投影的狀態：
                        # 先前只有工作串一行字，畫面上跟正常工作一模一樣。
                        rate_state[aid] = "alert" if ev["label"].startswith("⏳") else "warn"
                        await sync_emote(aid)
                        if ev["label"] in warned:
                            continue
                        warned.add(ev["label"])
                    await office_event({"v": 1, "agent": aid, **ev})
        await asyncio.wait_for(pump(), timeout=CLI_TIMEOUT)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await office_event({"v": 1, "agent": aid, "kind": "done", "label": "error",
                            "detail": f"CLI 超過 {int(CLI_TIMEOUT)} 秒未收工，已中止"})
        done_sent = True
    except asyncio.CancelledError:
        proc.kill()                      # 老闆按了中止
        await office_event({"v": 1, "agent": aid, "kind": "done", "label": "error",
                            "detail": "老闆中止了這個任務"})
        done_sent = True
        raise
    finally:
        cli_procs.pop(aid, None)
        if not done_sent:
            # CLI 沒吐 result 就死了（崩潰、被殺、輸出壞掉）。不補這一筆的話，
            # 卡片會永遠停在「進行中」、NPC 永遠不回座位——watchdog 五分鐘後才兜底。
            err = ""
            if proc.stderr is not None:
                err = (await proc.stderr.read())[-200:].decode("utf-8", "replace").strip()
            await office_event({"v": 1, "agent": aid, "kind": "done", "label": "error",
                                "detail": f"CLI 異常結束（退出碼 {proc.returncode}）{('：' + err) if err else ''}"})


# ── 模型選擇（觀察 ③：外殼要能臨時換模型）─────────────────────────────
# 清單資料驅動：OFFICE_MODELS 有設就用它，否則就是【人設裡實際指派過的那些】——
# 不在程式裡寫死一張會過期的型號表（cogito 也不驗證 model id，打錯只會讓下個任務
# 報錯燒掉一輪，所以外殼給選單、不給自由輸入）。
MODEL_RESET = "reset"   # 與聊天端 `model reset` 同一個字：把臨時覆蓋收回啟動預設

# 引擎：cogito（API 計費）or cli（Claude Code，用你已經在付的訂閱）。
# 與 model 同一個設計——是【員工的屬性】（persona 的 engine 欄位），外殼可臨時覆蓋。
ENGINE_COGITO, ENGINE_CLI = "cogito", "cli"
engine_sent: dict[str, str] = {}   # aid -> 外殼最後選的引擎（隨 state 持久化）


def engine_of(aid: str, override: str = "") -> str:
    """這次要用哪個引擎：外殼的選擇 > 人設 > 預設 cogito。CLI 不可用時一律退回 cogito
    ——選單本來就不會給那個選項，但 API 直呼進來也不能讓它炸。"""
    want = (override or engine_sent.get(aid) or
            (agents[aid].engine if aid in agents else "") or ENGINE_COGITO)
    return ENGINE_CLI if (want == ENGINE_CLI and cli_available()) else ENGINE_COGITO
model_sent: dict[str, str] = {}   # aid -> 橋最後一次告訴 cogito 的模型（隨 state 持久化）


def known_models() -> list[dict]:
    """後備清單：OFFICE_MODELS 有設就用它，否則就是人設裡實際指派過的那些。
    只有在【問不到 cogito】時才會走到——真正的清單來自官方（見 office_models）。"""
    if env := os.environ.get("OFFICE_MODELS", "").strip():
        ids = [m.strip() for m in env.split(",") if m.strip()]
    else:
        ids = sorted({a.model for a in agents.values() if a.model})
    return [{"id": i, "name": i} for i in ids]


_api_models: tuple[list[dict], float] = ([], 0.0)   # (清單, 抓到的時間)；6 小時內沿用


async def api_models() -> list[dict] | None:
    """橋【自己】問 Anthropic 的 /v1/models。

    為什麼需要這條：CLI 模式根本不經過 cogito，清單卻綁著 cogito 開不開——實際回報過
    「可以選的模型不只 haiku/opus」。橋本來就有 API key（意圖判斷在用），而列模型是
    免費的 GET（不耗 token），沒有理由不自己問。

    快取 6 小時：模型發布是以週計的事。抓失敗就沿用舊的——一份稍舊的清單，
    遠比因為網路抖一下就少掉一半選項有用。
    """
    global _api_models
    if client is None:
        return None
    cached, at = _api_models
    if cached and time.time() - at < 6 * 3600:
        return cached
    try:
        page = await client.models.list(limit=100)
    except Exception as e:      # SDK 的錯誤型別不只一種，這裡不值得逐一列舉
        print(f"⚠ 問不到官方模型清單（{type(e).__name__}）")
        return cached or None
    got = [{"id": m.id, "name": getattr(m, "display_name", "") or m.id} for m in page.data]
    if got:
        _api_models = (got, time.time())
    return got or cached or None


async def cogito_models() -> tuple[list[dict], str] | None:
    """問 cogito「現在真正能用哪些模型」（它再問 Anthropic 的 /v1/models，帶快取）。
    問不到回 None——清單是加值層，拿不到就降級，不讓選單整個消失。"""
    if not COGITO_HTTP:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as cl:
            r = await cl.get(f"{COGITO_HTTP}/models",
                             headers={"Authorization": f"Bearer {COGITO_HTTP_TOKEN}"})
        r.raise_for_status()
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"⚠ 問不到 cogito 的模型清單（用後備清單）：{type(e).__name__}")
        return None
    ms = [m for m in (d.get("models") or []) if isinstance(m, dict) and m.get("id")]
    return (ms, str(d.get("source") or "live")) if ms else None


@app.get("/office/models")
async def office_models():
    """外殼的模型選單。effective：這位員工現在【實際會用】哪個（人設 or 臨時覆蓋）。

    清單優先問 cogito（→ 官方 /v1/models）——手動維護的表必然落後於發布。問不到才用
    後備清單，並用 source 講清楚是哪一種，別讓降級變成無聲的。

    覆蓋是有記憶的（cogito 那邊 session 級持久），所以要把它揭露出來——不然選一次 opus
    就永遠是 opus，而畫面上看不出來，那就是隱形狀態。
    ⚠ effective 只反映【橋送出去的】：有人在 Slack 用 `model` 指令改過，這裡不會知道。
    """
    # 三段來源，由準到粗：cogito（它知道自己的 provider 支援什麼）→ 橋自己問官方
    # → 本地後備。source 一路講出來，降級不能是無聲的。
    if got := await cogito_models():
        models, source = got
    elif mine := await api_models():
        models, source = mine, "api"
    else:
        models, source = known_models(), "local"
    return {"ok": True, "models": models, "source": source, "reset": MODEL_RESET,
            "effective": {aid: model_sent.get(aid) or a.model for aid, a in agents.items()},
            # 引擎：CLI 找不到就不給這個選項（入口資料驅動，跟 repo 那排同一個原則）
            "cli": cli_available(), "cli_cmd": CLI_CMD,
            "engines": {aid: engine_of(aid) for aid in agents},
            "cli_models": dict(cli_model)}   # CLI 上次實際跑的模型（揭露，不是可設定值）


@app.post("/office/dispatch")
async def office_dispatch(d: dict):
    """Web 外殼派工/審批 → 轉發 cogito HTTP 入口（token 在橋端，瀏覽器拿不到）。"""
    global _dirty
    aid, text = d.get("agent", ""), (d.get("text") or "").strip()
    if aid not in agents or not text:
        return {"ok": False, "error": "缺 agent 或 text"}
    verb = text.split()[0]
    # 防呆：工作中不收新任務（cogito 也會擋，這裡先給即時回饋）。
    # approve/reject/stop/steer 是任務【進行中】的互動，一律放行——擋住中止等於沒有中止，
    # 擋住插話等於把「糾正走偏」的唯一選項留給「殺掉重來」。
    if verb in ("approve", "reject") and (src := approval_from(aid)):
        return {"ok": False, "error": f"這張審批來自 {src}，請回 {src} 核准（cogito 按頻道解析審批）"}
    if aid in busy and verb not in ("approve", "reject", "/stop", "/steer"):
        return {"ok": False, "error": f"{agents[aid].name} 正在工作中，收工後再派新任務"}
    # 插話只在工作中有意義。閒著時不代發成新任務——那會把「糾正」靜默升級成「開工」。
    if verb == "/steer":
        if aid not in busy:
            return {"ok": False, "error": f"{agents[aid].name} 沒在工作中，插不了話——直接派任務就好"}
        if not text[len("/steer"):].strip():
            return {"ok": False, "error": "插話是空的——/steer 後面要接要補的那句話"}
    # 引擎分流：CLI 模式不經過 cogito——它自己就是完整的 agent，橋只負責把它的事件
    # 轉成 office 事件（走位/泡泡/工作串/卡片全部共用同一條投影路徑）。
    cli_mode = engine_of(aid, str(d.get("engine") or "")) == ENGINE_CLI
    # 中止在分流【之前】處理：兩種引擎共用同一條收尾，差別只在「怎麼叫停上游」。
    # 這條路徑不准有任何 return False 的分支——中止按下去就得結束，這是使用者的決定。
    if verb == "/stop":
        card = last_report.get(aid)
        # busy 沒了但卡還開著（上個行程留下的、手打事件開的）也要能收——那正是先前
        # 唯一無解的情況：畫面顯示進行中，中止卻說「沒有進行中的任務」。
        if aid not in busy and not (card and card["status"] == "working"):
            return {"ok": False, "error": f"{agents[aid].name} 沒有進行中的任務"}
        if proc := cli_procs.get(aid):
            proc.kill()      # CLI 沒有優雅中止的入口，砍掉就是砍掉——done 由 finally 補
            how = ""
        elif cli_mode:
            how = "（沒有進行中的 CLI 行程——直接收掉這張卡）"
        elif COGITO_HTTP:
            how = await tell_cogito_stop(aid)
        else:
            how = "（未設 COGITO_HTTP——畫面收掉了，但沒叫停任何東西）"
        await force_stop(aid, how)
        return {"ok": True, "stopped": True}
    # 駁回跟中止同一個道理：它是使用者的【決定】，不是對上游的請求。所以先收卡再轉發，
    # 送不到也照收——逾時的預設行為【本來就是自動拒絕】，結果一致，畫面早一步反映事實
    # 不算說謊。這也保證審批卡永遠有出路：不會再出現「按了駁回卻收不掉」。
    #
    # 核准【刻意不比照】。審批擋的是高危操作：送不出去卻把卡收掉，使用者會以為
    # rm -rf 已經授權執行了，實際上 agent 會等到逾時然後【自動拒絕】——那是相反的結果。
    # 寧可卡留著、明講送不出去（見下面轉發失敗的訊息）。
    if verb == "reject" and aid in pending_approval:
        how = await tell_cogito_reject(aid)
        clear_approval(aid)
        await sync_emote(aid)                # 頭上的倒數餅圖跟著收
        notify("agent", aid, alert="done")
        await bubble(aid, "⚠ 駁回")
        log_ev(aid, f"🧑‍💼 老闆駁回了這個操作{how}")
        if desk := WORK_DESK.get(aid):       # 審批完回工位繼續
            await goto(aid, desk)
        return {"ok": True, "delivered": not how}

    if cli_mode and verb not in ("approve", "reject", "/stop", "/steer"):
        # 只記外殼的選擇；班表派工（scheduled）指定的引擎是那件事的屬性，不是老闆對這個人的決定
        if (eng := str(d.get("engine") or "")) and not d.get("scheduled"):
            engine_sent[aid] = eng
            _dirty = True
        # repo 綁定要在【分流之前】做——先前這裡直接 return，於是 CLI 模式選了 repo
        # 等於沒選：worktree 沒開、任務文字沒帶說明，CLI 就在頻道工作區裡跑，
        # 然後合理地認定自己在 cogito-agent（實際回報過的症狀）。
        wt = None
        if rname := str(d.get("repo") or ""):
            repo = next((r for r in local_repos() if r["name"] == rname), None)
            if repo is None:
                return {"ok": False, "error": f"不認識的 repo：{rname}（清單見工作 repo 選單）"}
            bound = bind_repo(aid, repo)
            if isinstance(bound, str):
                return {"ok": False, "error": bound}
            wt = worktree_path(aid, bound[0])
            # CLI 直接在 worktree 裡跑，所以措辭與 cogito 版不同：不必叫它 cd 進去，
            # 但「不要 push、不要碰原目錄」這條對誰都一樣。
            text = (f"{text}\n\n【工作 repo】你現在就在 {repo['path']} 的 git worktree 裡"
                    f"（分支 {bound[1]}，與原 repo 共用歷史）。改完 commit 到這個分支即可，"
                    "【不要 push、不要碰原目錄】——老闆會自己驗收合併。")
        # 模型：與 cogito 同一套優先序（外殼選的 > 人設）。「還原預設」＝不帶 --model，
        # 交回 CLI 自己的設定。選了就記下來（跟 cogito 那條共用 model_sent，兩邊語意一致）。
        pick = str(d.get("model") or "").strip()
        if pick:
            model_sent[aid] = "" if pick == MODEL_RESET else pick
            _dirty = True
        cli_want = "" if pick == MODEL_RESET else (pick or model_sent.get(aid) or agents[aid].model)
        asyncio.create_task(run_cli_task(aid, text, wt, cli_want))
        return {"ok": True, "engine": ENGINE_CLI, "repo": bool(wt), "model": cli_want}
    if cli_mode:
        return {"ok": False, "error": f"CLI 模式不支援「{verb}」——審批與插話是 cogito 的機制"}
    if not COGITO_HTTP:
        return {"ok": False, "error": "未設 COGITO_HTTP——cogito 的 HTTP 派工入口未啟用"}
    # 人數上限只對看板有意義（其他人本來就是一個人做），而且不能套在 approve/reject//stop
    # 那些【任務進行中】的互動上——那會把一句 "approve" 變成一段新指令。
    people = d.get("people")
    if aid == KANBAN and verb not in ("approve", "reject", "/stop", "/steer") and isinstance(people, int):
        if not 1 <= people <= len(npcs()):
            return {"ok": False, "error": f"參與人數要在 1–{len(npcs())} 之間"}
        text = with_headcount(text, people)
    # repo 綁定：白名單比對 /office/repos 的清單，不吃路徑（跟封存板的 f= 同一個安全原則）
    if (rname := d.get("repo")) and verb not in ("approve", "reject", "/stop", "/steer"):
        repo = next((r for r in local_repos() if r["name"] == rname), None)
        if repo is None:
            return {"ok": False, "error": f"不認識的 repo：{rname}（清單見工作 repo 選單）"}
        bound = bind_repo(aid, repo)
        if isinstance(bound, str):
            return {"ok": False, "error": bound}
        text = with_repo(text, bound[0], repo["path"], bound[1])
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            # model：員工的屬性（persona 的 model 欄位），跟任務一起送。空＝不動 cogito
            # 那邊現有的設定（可能是聊天端 `model` 指令設的），別無聲覆蓋人家的選擇。
            payload = {"agent": aid, "text": text}
            # 優先序：外殼這次選的 > 人設。都沒有就【不送這個鍵】——送空字串會把
            # 使用者在聊天端用 `model` 指令選的無聲清掉（要收回覆蓋請明選「還原預設」）。
            if m := (str(d.get("model") or "").strip() or agents[aid].model):
                payload["model"] = m
                model_sent[aid] = "" if m == MODEL_RESET else m
                _dirty = True
            r = await cl.post(f"{COGITO_HTTP}/task", json=payload,
                              headers=cogito_headers(text))
    except httpx.HTTPError as e:
        return {"ok": False, "error": approve_hint(verb, f"cogito 入口連不上：{type(e).__name__}")}
    if r.status_code != 202:
        return {"ok": False, "error": approve_hint(verb, f"cogito 回 {r.status_code}：{r.text[:120]}")}
    if verb == "/steer":
        # 投影：插話上工作串（卡片正開著，直接掛進去）＋泡泡。cogito 端下一輪生效。
        log_ev(aid, f"🧑‍💼 老闆插話：{text[len('/steer'):].strip()[:200]}")
        await bubble(aid, "📨 插話")
    elif verb in ("approve", "reject"):
        clear_approval(aid)  # cogito 確認收到才收卡
        await sync_emote(aid)                # 決定做了，頭上的問號立刻收（不等 sweep）
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


# ── 班表（Devin 對照筆記 ③）：辦公室的例行任務——保全每週巡 repo 之類 ──────────
# 掛在橋而不是 cogito 的 cron：走一般派工路徑，走位/工作串/報告卡全部免費，
# 而且班表是「辦公室的制度」，不是「大腦的排程」。格式見 schedule.json.example。
SCHEDULE_FILE = Path(__file__).parent / "schedule.json"
sched_last: dict[str, str] = {}   # job name -> 上次觸發的 "YYYY-MM-DD HH"（防同一小時重複；隨 state 持久化）


def load_schedule() -> list[dict]:
    if not SCHEDULE_FILE.exists():
        return []
    try:
        jobs = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        return jobs if isinstance(jobs, list) else []
    except ValueError as e:
        print(f"⚠ schedule.json 壞了，班表停用：{e}")
        return []


async def run_due_jobs(now: time.struct_time) -> None:
    """weekday（0=週一；省略＝每天）＋hour 命中、這一小時還沒跑過 → 派工。job 可帶 engine（cli／cogito）。

    人在忙就【跳過這一輪】而不是排隊——班表任務是例行巡邏，錯過一輪下次照排；
    排隊反而會在他收工的瞬間搶走老闆正要派的活。跳過有留痕，不是靜默消失。
    """
    global _dirty
    stamp = time.strftime("%Y-%m-%d %H", now)
    for job in load_schedule():
        name, aid = str(job.get("name", "")), str(job.get("agent", ""))
        if not name or aid not in agents or not str(job.get("text", "")).strip():
            continue
        if job.get("weekday") not in (None, now.tm_wday) or job.get("hour") != now.tm_hour:
            continue
        if sched_last.get(name) == stamp:
            continue
        sched_last[name] = stamp
        _dirty = True
        if aid in busy:
            log_ev(aid, f"🗓 班表任務「{name}」到點，但人在忙——這輪跳過，下次照排")
            continue
        # engine 是【這件例行事】的屬性（省錢的走 CLI、要審批的走 cogito），不是這位員工的；
        # 所以帶 scheduled 標記，dispatch 才不會把它記成「外殼最後選的引擎」。
        r = await office_dispatch({"agent": aid, "text": job["text"], "repo": job.get("repo"),
                                   "engine": job.get("engine"), "scheduled": True})
        log_ev(aid, f"🗓 班表任務「{name}」開跑（由班表觸發，不是老闆派的）" if r.get("ok")
               else f"🗓 班表任務「{name}」派不出去：{r.get('error')}")


async def schedule_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await run_due_jobs(time.localtime())
        except Exception as e:  # 班表壞了不能拖垮橋——記一筆，下一分鐘再試
            print(f"⚠ 班表迴圈出錯：{e}")


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
              # 名冊也要看得到頭上那些徽章。理由不只是方便：
              #   ① 看板【沒有身體】，它那份（實測 33 條待審提案，佔全部六成）在 3D 畫面上
              #      根本掛不出來——emote() 對 KANBAN 直接 return；
              #   ② 沒建 WebGL 時整個 3D 是空的，但 README 說好名冊照常運作。
              # 來源跟徽章同一個 want_emote()，不另外發明一套判斷。
              "badge": want_emote(aid),
              # memo 另外給【數字】而不是只給 badge：頭上一次只掛得下一件事（有優先序），
              # 名冊有位子並排——「他在等審批」跟「他還有 6 條提案沒人收」可以同時為真。
              "memo": memo_pending.get(aid, 0),
              "npc": aid != KANBAN,   # False＝這張卡沒有身體（看板），前端不畫走位/位置
            "model": a.model,      # 設定值（空＝跟 cogito 啟動預設）；實際跑的那個看卡片
              "memory": list(a.memory)}
        for aid, a in agents.items()
    }


# ── 持久化：任務卡/黏性指派/審批卡落地 JSON——橋重啟不失憶（cogito 的 session 本來就落地，
# 這邊補齊對稱）。notify() 兼作 dirty 標記，存檔器每 2 秒批次寫（原子寫入：tmp + rename）。
STATE_FILE = Path(os.environ.get("OFFICE_STATE") or Path(__file__).parent / "office_state.json")

# ── 人設落地：把 personas/<id>.md 同步進各頻道的工作目錄，同一份內容寫兩個檔名。
# cogito 的 PromptComposer 讀 AGENTS.md；Claude Code 讀 CLAUDE.md（含 cwd 的上層目錄，所以綁 repo
# 在 worktree 裡跑也吃得到）、【不讀 AGENTS.md】——2026-09-07 探針實測：同目錄放兩份，CLI 逐字列出
# CLAUDE.md、對 AGENTS.md 回「無」；老徐工作區直接問「你是誰」答「沒有這類資訊」。先前只寫 AGENTS.md，
# 於是走 CLI 的員工歷來都是無人設狀態，只是沒人問過他們。這一步讓角色設定【真的影響 agent 行為】，
# 而不只是名冊上的一張名片。沒設 COGITO_CHANNELS 就整個不啟用——這是往別的 repo 的工作區寫檔，預設關閉。
CHANNELS_DIR = Path(os.environ["COGITO_CHANNELS"]).expanduser() if os.environ.get("COGITO_CHANNELS") else None
SOUL_MARK = "<!-- office-persona:"   # 我們產生的檔案的第一行；手寫的沒有它
SOUL_FILES = ("AGENTS.md", "CLAUDE.md")   # 前者 cogito 讀、後者 Claude Code 讀；內容同源


def agents_dir() -> Path | None:
    """具名 agent 要寫去哪。

    ⚠ 這【不是】各頻道的工作目錄。cogito 的 spawn_subagent 是用 SkillsBaseDir=rootDir 建
    AgentLoader 的（cmd/claw/main.go 的 WireSubagent），也就是【共享根】的 .claw/agents/。
    先前寫進 channels/office_kanban/.claw/agents/，那個目錄從來沒被讀過——於是主持人明明
    被守則要求用人名，卻怎麼點都點不到，只能退回真正存在的 implementer/planner。
    它不是不聽話，是我把檔案放在它看不到的地方。

    COGITO_AGENTS_DIR 可覆蓋；預設由 COGITO_CHANNELS 往上一層推（channels 就在 workspace 底下）。
    """
    if env := os.environ.get("COGITO_AGENTS_DIR"):
        return Path(env).expanduser()
    return CHANNELS_DIR.parent / ".claw" / "agents" if CHANNELS_DIR else None


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
    """把六個人設投影成 cogito 的【具名 agent】（.claw/agents/<名字>.md），
    讓主持人能用 spawn_subagent 點名真正的人設，而不是在 task_prompt 裡臨時捏一個。

    寫進【共享根】而不是 kanban 頻道——見 agents_dir() 的說明。原本想「只有協作模式需要
    點名，所以只給 kanban」，但 cogito 的 AgentLoader 是共享的，那個分界在它那邊不存在；
    硬要分的結果就是檔案放在沒人讀的地方。這裡讓步給既有架構，不是設計取捨。
    沿用 SOUL_MARK 保護：沒有標記的檔案是人寫的，不覆蓋。"""
    dst_dir = agents_dir()
    if dst_dir is None:
        return 0
    persona_dir = Path(__file__).parent / "personas"
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
    """回傳 {寫入, 無異動, 略過}，兩個檔名合計。略過＝那個檔是人寫的，我們不覆蓋。"""
    n = {"wrote": 0, "same": 0, "skipped": 0}
    if CHANNELS_DIR is None:
        return n
    for aid in agents:
        src = Path(__file__).parent / "personas" / f"{aid}.md"
        if not src.exists():
            continue
        want = soul_doc(aid, src.read_text(encoding="utf-8"))
        for fname in SOUL_FILES:
            _sync_one(CHANNELS_DIR / f"office_{aid}" / fname, want, n)
    if any(n.values()):
        print(f"人設同步 AGENTS.md＋CLAUDE.md：寫入 {n['wrote']}、已是最新 {n['same']}、略過手寫 {n['skipped']}")
    return n


def _sync_one(dst: Path, want: str, n: dict[str, int]) -> None:
    if True:
        if dst.exists():
            old = dst.read_text(encoding="utf-8")
            if not old.startswith(SOUL_MARK):   # ⚠ 保護：手寫的一律不動
                n["skipped"] += 1
                print(f"⚠ {dst} 不是由人設產生的（沒有標記），保留原檔不覆蓋")
                return
            if old == want:
                n["same"] += 1
                return
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(want, encoding="utf-8")
        n["wrote"] += 1


def save_state() -> None:
    global _dirty
    _dirty = False
    data = {"history": {a: list(cards) for a, cards in history.items()},
            "conv_npc": conv_npc, "pending_approval": pending_approval,
            "approval_src": approval_src,
            "approval_meta": approval_meta, "approval_at": approval_at,
            "sched_last": sched_last,  # 班表防重：重啟不能讓同一小時的巡邏跑兩次
            "model_sent": model_sent,
            "engine_sent": engine_sent,  # 引擎覆蓋也是長期狀態，重啟後畫面不能忘記
            # CLI 回報的能力與模型也要跟著走：它們只在【跑過任務】時才拿得到，
            # 不存的話每次重啟能力面板就空白，得先派一次工才看得到（實際回報）。
            "cli_caps": cli_caps, "cli_model": cli_model}  # 模型覆蓋是 cogito session 級的，重啟後畫面不能忘記
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
    approval_meta.update(data.get("approval_meta", {}))
    approval_at.update(data.get("approval_at", {}))
    sched_last.update(data.get("sched_last", {}))
    model_sent.update(data.get("model_sent", {}))
    engine_sent.update(data.get("engine_sent", {}))
    if isinstance(saved := data.get("cli_caps"), dict) and saved.get("tools"):
        cli_caps.update(saved)
    cli_model.update(data.get("cli_model", {}))
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
    # 提案數要在【第一次開名冊之前】就是對的。只靠 sweep（30 秒一輪）的話，剛啟動那段
    # 名冊會說「0 條」——那不是「還沒載入」，是一句錯的話（實際上看板就有 33 條）。
    refresh_proposed()
    # watchdog 脫鉤 Unity：純 Web 派工（不開 Unity）失聯保險也要在
    global watchdog
    if watchdog is None or watchdog.done():
        watchdog = asyncio.create_task(work_watchdog())
    asyncio.create_task(state_saver())
    asyncio.create_task(schedule_loop())   # 班表：例行任務（schedule.json，沒檔就整輪 no-op）


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

    /avatars 用 no-cache 而不是 no-store：差別是「還能存，但每次都要回頭問」。
    新同事上線時人設先落地、頭像後產（兩支腳本），中間那個空窗只要有人載過名冊，
    瀏覽器就把那張 404 記起來——之後怎麼重新整理都是文字頭像，看起來像「這個人沒有臉」。
    no-cache 讓它每次帶 If-Modified-Since 回頭問一次，沒變就回 304（無 body），
    在本機幾乎沒有成本，換掉的是一整類「明明檔案在、畫面就是不更新」的偵錯。
    """
    resp = await call_next(request)
    if request.url.path.startswith(("/unity", "/shell")):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    elif request.url.path.startswith("/avatars"):
        resp.headers["Cache-Control"] = "no-cache"
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

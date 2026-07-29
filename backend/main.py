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
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from agent import Agent, build_tools

load_dotenv()  # 讀 backend/.env（ANTHROPIC_API_KEY=...），已 gitignore

app = FastAPI()
unity: WebSocket | None = None
events: list[dict] = []
agents: dict[str, Agent] = {}
arrived: dict[str, asyncio.Event] = {}
loops: list[asyncio.Task] = []
waypoint_list: list[str] = []
occupied: dict[str, str] = {}   # agent_id -> 佔用的 waypoint（防兩人擠同一點）
busy: set[str] = set()          # 對話/工作中的 agent（主迴圈掛起）
watchdog: asyncio.Task | None = None  # 工作失聯巡查（start_agents 時啟動）

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
    global unity
    await ws.accept()
    unity = ws
    print("✓ Unity 已連線")
    try:
        while True:
            evt = json.loads(await ws.receive_text())
            events.append(evt)
            await handle_event(evt)
    except WebSocketDisconnect:
        unity = None
        stop_agents()
        print("✗ Unity 斷線")


async def handle_event(evt: dict) -> None:
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


def start_agents(agent_ids: list[str], waypoints: list[str]) -> None:
    global waypoint_list
    stop_agents()
    waypoint_list = waypoints
    persona_dir = Path(__file__).parent / "personas"
    for aid in agent_ids:  # 先全部建好，才知道彼此名字
        agents[aid] = Agent(aid, persona_dir)
        arrived[aid] = asyncio.Event()
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
    print(f"啟動 {len(agent_ids)} 個 agent（{mode}），{len(waypoints)} 個互動點")


def stop_agents() -> None:
    for t in loops:
        t.cancel()
    for t in pacers.values():
        t.cancel()
    loops.clear()
    agents.clear()
    arrived.clear()
    occupied.clear()
    busy.clear()
    sub_active.clear()
    work_last.clear()
    bubble_q.clear()
    pacers.clear()
    conv_npc.clear()


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
    if unity is None:
        return False
    await unity.send_text(json.dumps(cmd, ensure_ascii=False))
    return True


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
    while unity is not None:
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
            if unity is None:
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
SUB_RE = re.compile(r"^\[Subagent(?::([^\]]+))?\]\s*")  # cogito 子 agent 事件前綴


def say(aid: str, text: str) -> dict:
    return {"agent_id": aid, "action": "say", "channel": "public", "text": text}


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
    """收工/失聯：釋放主 agent 與其名下所有委派卡。"""
    work_last.pop(aid, None)
    busy.discard(aid)
    for key in [k for k in sub_active if k[0] == aid]:
        busy.discard(sub_active.pop(key))


async def sweep_work() -> None:
    now = time.monotonic()
    for aid in [a for a, t in list(work_last.items()) if now - t > WORK_TIMEOUT]:
        print(f"⚠ {aid} 上工中 {WORK_TIMEOUT:.0f}s 無事件，視為失聯，釋放")
        release_work(aid)
        if aid in agents:
            agents[aid].remember("工作任務失聯中斷了")
            await bubble(aid, "✗ 任務失聯中斷")


async def work_watchdog() -> None:
    while True:
        await asyncio.sleep(WATCH_TICK)
        await sweep_work()


def office_bubble(kind: str, label: str) -> str | None:
    """事件 → NPC 頭上泡泡文字；None＝這種事件不冒泡。"""
    sub = SUB_RE.match(label)
    if sub:  # 走到這代表委派沒開成卡（沒人有空）：退回主 agent 冒泡帶小名
        label = f"{sub.group(1) or '手下'}·{SUB_RE.sub('', label)}"
    if kind == "start":
        return f"📋 {label[:40]}"
    if kind == "tool":
        return f"▸ {label[:40]}"
    if kind == "error":
        return f"✗ {label[:40]}"
    if kind == "msg":
        return label[:60]
    if kind == "done":
        return "✔ 任務完成" if label == "ok" else "✗ 任務中斷"
    return None


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
            desk = WORK_DESK.get(npc)
            if desk:
                occupied[npc] = desk
                await send_cmd({"agent_id": npc, "action": "move_to", "target": desk})
            await bubble(parent, f"🤝 委派 {shown}")
            await bubble(npc, f"📋 支援{agents[parent].name}：{shown}")
            agents[npc].remember(f"接下{agents[parent].name}委派的{shown}工作")
            return True
        if kind in ("result", "error"):  # 委派收工
            npc = sub_active.pop((parent, name), None)
            if npc is None:
                return False
            busy.discard(npc)
            mark = "✔ 回報：" if kind == "result" else "✗ 失敗："
            await bubble(npc, f"{mark}{detail[:36]}")
            agents[npc].remember("完成了委派工作" if kind == "result" else "委派的工作失敗了")
            return True
        return True  # spawn 的其他事件不投影
    sub = SUB_RE.match(label)
    if sub:
        npc = sub_active.get((parent, sub.group(1) or ""))
        if npc is None:
            return False  # 沒開成卡：退回主 agent 帶小名冒泡
        text = office_bubble(kind, SUB_RE.sub("", label))
        if text:
            await bubble(npc, text, collapsible=kind == "tool")
        return True
    return False


# Slack/Telegram 常駐派工（cogito chatbot Core）：事件的 agent 是頻道 id（"slack:C123"）
# 而非 persona id。未知 id 動態指派一個閒置 NPC，黏性映射——同頻道固定同員工，橋重啟才重配。
conv_npc: dict[str, str] = {}  # 頻道 id -> persona id


def resolve_npc(ext: str) -> str | None:
    if ext in agents:  # claw-cli 直接指名 persona，原路
        return ext
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
    if unity is None or aid is None:
        return {"ok": False, "error": "Unity 未連線或沒有可指派的 NPC"}
    a = agents[aid]

    if aid in work_last or kind == "start":  # 上工中任何事件（含 think/turn）都算心跳
        work_last[aid] = time.monotonic()

    if await project_sub(aid, kind, label, ev.get("detail", "")):
        return {"ok": True}

    if kind == "start":  # 上工：掛起生活模擬、走到工位（自動入座），任務泡
        busy.add(aid)
        desk = WORK_DESK.get(aid)
        if desk:
            occupied[aid] = desk
            await send_cmd({"agent_id": aid, "action": "move_to", "target": desk})
        a.remember(f"接到工作任務「{label}」，開始上工")
    elif kind == "done":  # 收工：釋放主 agent＋名下委派卡，回歸 idle
        release_work(aid)
        a.remember("完成了手上的工作任務" if label == "ok" else "工作任務中斷了")

    text = office_bubble(kind, label)
    if text:
        await bubble(aid, text, collapsible=kind == "tool")
    return {"ok": True}


@app.post("/cmd")
async def cmd(c: dict):
    if unity is None:
        return {"ok": False, "error": "Unity 未連線"}
    await unity.send_text(json.dumps(c, ensure_ascii=False))
    return {"ok": True, "sent": c}


@app.get("/events")
def get_events():
    return events[-20:]


@app.get("/agents")
def get_agents():
    return {
        aid: {"name": a.name, "location": a.location, "busy": aid in busy,
              "memory": list(a.memory)}
        for aid, a in agents.items()
    }

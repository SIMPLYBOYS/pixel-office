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
busy: set[str] = set()          # 對話中的 agent（主迴圈掛起）

client = anthropic.AsyncAnthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
if client is None:
    print("⚠ 未設定 ANTHROPIC_API_KEY——agent 將隨機走動（合約測試模式）")

DECISION_INTERVAL = (8.0, 25.0)  # 決策間隔秒數範圍（拉長省成本、縮短加快節奏）
MAX_ROUNDS = 5                   # 對話回合上限（指南 §3：4~6 輪強制結束）
BUBBLE_WAIT = 2.8                # 每句話的展示間隔（等泡泡讀完）


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
    mode = "Claude 決策" if client else "隨機走動（無 API key）"
    print(f"啟動 {len(agent_ids)} 個 agent（{mode}），{len(waypoints)} 個互動點")


def stop_agents() -> None:
    for t in loops:
        t.cancel()
    loops.clear()
    agents.clear()
    arrived.clear()
    occupied.clear()
    busy.clear()


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
            if client is not None:
                actions = await a.decide(client, tools, others_desc(a))
            else:
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

        await asyncio.sleep(random.uniform(*DECISION_INTERVAL))


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

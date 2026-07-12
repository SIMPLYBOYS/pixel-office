"""多 Agent 大腦後端（架構指南第 3 節落地）。

跑法：
  .venv/bin/uvicorn main:app --port 8123     # 在 backend/ 目錄下
  需要 ANTHROPIC_API_KEY（沒有的話 agent 會退化成隨機走動，管線照樣通）

流程：
  Unity 連上 /ws → 送握手 {"type":"waypoints","agents":[...],"list":[...]}
  → 每個 agent 起一個決策迴圈：decide()（Claude 決策）→ 推指令給 Unity
  → Unity 回報 arrived → 寫進該 agent 記憶 → 決策間隔後再 decide()

手動介入（跳過大腦直接下指令）照舊：
  curl -X POST localhost:8123/cmd -H 'Content-Type: application/json' \
       -d '{"agent_id":"p17","action":"move_to","target":"cooler_1"}'
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

client = anthropic.AsyncAnthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
if client is None:
    print("⚠ 未設定 ANTHROPIC_API_KEY——agent 將隨機走動（合約測試模式）")

DECISION_INTERVAL = (8.0, 25.0)  # 決策間隔秒數範圍（拉長省成本、縮短加快節奏）


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
    tools = build_tools(waypoints)
    persona_dir = Path(__file__).parent / "personas"
    for aid in agent_ids:
        agents[aid] = Agent(aid, persona_dir)
        arrived[aid] = asyncio.Event()
        loops.append(asyncio.create_task(agent_loop(agents[aid], tools)))
    mode = "Claude 決策" if client else "隨機走動（無 API key）"
    print(f"啟動 {len(agent_ids)} 個 agent（{mode}），{len(waypoints)} 個互動點")


def stop_agents() -> None:
    for t in loops:
        t.cancel()
    loops.clear()
    agents.clear()
    arrived.clear()
    occupied.clear()


occupied: dict[str, str] = {}  # agent_id -> 佔用的 waypoint（防兩人擠同一點）


def others_desc(me: Agent) -> str:
    parts = []
    for aid, other in agents.items():
        if aid != me.id:
            parts.append(f"{other.persona.get('name', aid)}在{other.location}")
    return "，".join(parts)


async def agent_loop(a: Agent, tools: list[dict]) -> None:
    await asyncio.sleep(random.uniform(1.0, 6.0))  # 錯開起步
    while unity is not None:
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
            if act["action"] == "move_to":
                # 佔位守衛：目的地被同事佔用就換一個空位（大腦知道同事動態，理論上少踩；這是硬保險）
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

            cmd = {"agent_id": a.id, **act}
            await unity.send_text(json.dumps(cmd, ensure_ascii=False))
            print(f"{a.persona.get('name', a.id)}: {act}")
            if act["action"] == "move_to":
                a.remember(f"出發去{act.get('target')}")
                arrived[a.id].clear()
                try:
                    await asyncio.wait_for(arrived[a.id].wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    a.remember("剛剛走去某處超時沒走到")
            elif act["action"] == "say":
                a.remember(f"說了「{act.get('text')}」")

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
        aid: {"name": a.persona.get("name", aid), "location": a.location, "memory": list(a.memory)}
        for aid, a in agents.items()
    }

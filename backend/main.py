"""Step 4 合約驗證後端：手打指令 → Unity 執行 → 事件回報。

跑法：
  pip3 install fastapi 'uvicorn[standard]'
  uvicorn main:app --port 8123   # 在 backend/ 目錄下

手打指令（Unity 進 Play 後）：
  curl -X POST localhost:8123/cmd -H 'Content-Type: application/json' \
       -d '{"agent_id":"p17","action":"move_to","target":"cooler_1"}'
  curl -X POST localhost:8123/cmd -H 'Content-Type: application/json' \
       -d '{"agent_id":"p17","action":"use","target":"sit_up"}'
  curl localhost:8123/events     # 看 Unity 回報的 arrived 事件

合約（架構指南第 2 節）：
  後端 → Unity: {"agent_id","action","target","channel","text"}
  Unity → 後端: {"type":"arrived","agent_id","at"}

ponytail: 單一 Unity 連線、事件存記憶體。接 LLM decide() 時再拆 agent 迴圈（指南第 3 節）
"""
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()
unity: WebSocket | None = None
events: list[dict] = []


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
            print("事件:", evt)
    except WebSocketDisconnect:
        unity = None
        print("✗ Unity 斷線")


@app.post("/cmd")
async def cmd(c: dict):
    if unity is None:
        return {"ok": False, "error": "Unity 未連線"}
    await unity.send_text(json.dumps(c))
    return {"ok": True, "sent": c}


@app.get("/events")
def get_events():
    return events[-20:]

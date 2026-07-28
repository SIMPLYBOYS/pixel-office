"""Office 投影橋合約測試：假 Unity（TestClient WS）收指令，驗 /office/event 投影表。

跑法：.venv/bin/python test_office.py
不碰真 Unity、不叫 Claude API（client 強制 None + 生活迴圈全掛起）。
"""
import json
import time

import main
from fastapi.testclient import TestClient


def recv(ws) -> dict:
    return json.loads(ws.receive_text())


def post(c, **ev) -> dict:
    return c.post("/office/event", json=ev).json()


def run() -> None:
    main.client = None  # 測試絕不打真 API
    with TestClient(main.app) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({
                "type": "waypoints",
                "agents": ["p17", "p01", "p07"],
                "list": ["chair_1", "chair_2", "chair_3", "cooler_1"],
            }))
            for _ in range(50):  # 等握手處理完
                if len(main.agents) == 3:
                    break
                time.sleep(0.1)
            assert len(main.agents) == 3, "握手後 agents 未建立"
            main.busy.update(main.agents)  # 掛起所有生活迴圈 → WS 流只剩投影指令

            # 不認識的 agent → 拒收
            assert post(c, agent="p99", kind="start", label="x")["ok"] is False

            # start：走工位 + 任務泡 + 掛起
            assert post(c, agent="p17", kind="start", label="盤點 repo 的 TODO")["ok"]
            assert recv(ws) == {"agent_id": "p17", "action": "move_to", "target": "chair_1"}
            assert recv(ws)["text"] == "📋 盤點 repo 的 TODO"
            assert "p17" in main.busy and main.occupied["p17"] == "chair_1"

            # tool → ▸ 泡；think/turn/result 不投影（靠順序驗證：夾在中間不該出現）
            assert post(c, agent="p17", kind="think", label="")["ok"]
            assert post(c, agent="p17", kind="tool", label="bash", detail="grep -rn TODO")["ok"]
            assert recv(ws)["text"] == "▸ bash"

            # 子 agent 前綴 → 帶小名
            post(c, agent="p17", kind="tool", label="[Subagent:小美] read_file")
            assert recv(ws)["text"] == "▸ 小美·read_file"

            # error → ✗；msg → 內容泡
            post(c, agent="p17", kind="result", label="bash")  # 不投影
            post(c, agent="p17", kind="error", label="bash")
            assert recv(ws)["text"] == "✗ bash"
            post(c, agent="p17", kind="msg", label="TODO 共 3 處，已列清單")
            assert recv(ws)["text"] == "TODO 共 3 處，已列清單"

            # done → 收工泡 + 釋放
            main.busy.update(main.agents)  # done 會放人，先確保其他人仍掛起
            post(c, agent="p17", kind="done", label="ok")
            assert recv(ws)["text"] == "✔ 任務完成"
            assert "p17" not in main.busy
            assert any("接到工作任務" in m for m in main.agents["p17"].memory)

    print("✓ office 投影合約測試全過")


if __name__ == "__main__":
    run()

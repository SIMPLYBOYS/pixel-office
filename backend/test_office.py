"""Office 投影橋合約測試：假 Unity（TestClient WS）收指令，驗 /office/event 投影表。

跑法：.venv/bin/python test_office.py
不碰真 Unity、不叫 Claude API（生活迴圈整個 patch 掉，測試全確定性）。
"""
import json
import time

import main
from fastapi.testclient import TestClient


def recv(ws) -> dict:
    return json.loads(ws.receive_text())


def post(c, **ev) -> dict:
    return c.post("/office/event", json=ev).json()


async def _no_life(a, tools):  # 生活迴圈替身：測試只看投影指令
    pass


def run() -> None:
    main.client = None
    main.agent_loop = _no_life
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

            # 不認識的 agent → 拒收
            assert post(c, agent="p99", kind="start", label="x")["ok"] is False

            # start：走工位 + 任務泡 + 掛起
            assert post(c, agent="p17", kind="start", label="盤點 repo 的 TODO")["ok"]
            assert recv(ws) == {"agent_id": "p17", "action": "move_to", "target": "chair_1"}
            assert recv(ws)["text"] == "📋 盤點 repo 的 TODO"
            assert "p17" in main.busy and main.occupied["p17"] == "chair_1"

            # tool → ▸ 泡；think/turn/result 不投影（靠順序驗證：夾在中間不該出現）
            post(c, agent="p17", kind="think", label="")
            post(c, agent="p17", kind="tool", label="bash", detail="grep -rn TODO")
            assert recv(ws)["text"] == "▸ bash"

            # 沒開委派卡的子 agent 前綴 → 退回主 agent 帶小名
            post(c, agent="p17", kind="tool", label="[Subagent:神秘人] read_file")
            assert recv(ws)["text"] == "▸ 神秘人·read_file"

            # error → ✗；msg → 內容泡
            post(c, agent="p17", kind="result", label="bash")  # 不投影
            post(c, agent="p17", kind="error", label="bash")
            assert recv(ws)["text"] == "✗ bash"
            post(c, agent="p17", kind="msg", label="TODO 共 3 處，已列清單")
            assert recv(ws)["text"] == "TODO 共 3 處，已列清單"

            # 委派上工：spawn code-reviewer → 小美（p01）起身入座 + 雙泡 + 掛起
            post(c, agent="p17", kind="tool", label="spawn_subagent:code-reviewer",
                 detail='{"agent_type":"code-reviewer"}')
            assert recv(ws) == {"agent_id": "p01", "action": "move_to", "target": "chair_2"}
            m = recv(ws)
            assert (m["agent_id"], m["text"]) == ("p17", "🤝 委派 code-reviewer")
            m = recv(ws)
            assert (m["agent_id"], m["text"]) == ("p01", "📋 支援阿哲：code-reviewer")
            assert "p01" in main.busy

            # 委派中的內部事件 → 泡泡掛到小美頭上（前綴剝掉）
            post(c, agent="p17", kind="tool", label="[Subagent:code-reviewer] read_file")
            m = recv(ws)
            assert (m["agent_id"], m["text"]) == ("p01", "▸ read_file")

            # 委派收工：回報泡 + 釋放
            post(c, agent="p17", kind="result", label="spawn_subagent:code-reviewer",
                 detail="LGTM，無阻塞問題")
            m = recv(ws)
            assert (m["agent_id"], m["text"]) == ("p01", "✔ 回報：LGTM，無阻塞問題")
            assert "p01" not in main.busy
            assert any("委派" in x for x in main.agents["p01"].memory)

            # 無名子 agent（探路者）：兩側正規化成空名，一樣開卡
            post(c, agent="p17", kind="tool", label="spawn_subagent")
            assert recv(ws)["agent_id"] == "p01"  # 又輪到有空的小美
            assert recv(ws)["text"] == "🤝 委派 探路者"
            assert recv(ws)["text"] == "📋 支援阿哲：探路者"

            # 主任務收工：✔ 泡 + 釋放主 agent，順手收掉沒關的委派卡
            post(c, agent="p17", kind="done", label="ok")
            assert recv(ws)["text"] == "✔ 任務完成"
            assert "p17" not in main.busy and "p01" not in main.busy
            assert not main.sub_active
            assert any("接到工作任務" in x for x in main.agents["p17"].memory)

    print("✓ office 投影合約測試全過（含子 agent 映射）")


if __name__ == "__main__":
    run()

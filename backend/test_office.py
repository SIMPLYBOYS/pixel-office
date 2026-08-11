"""Office 投影橋合約測試：假 Unity（TestClient WS）收指令，驗 /office/event 投影表。

跑法：.venv/bin/python test_office.py
不碰真 Unity、不叫 Claude API（生活迴圈整個 patch 掉，測試全確定性）。
"""
import asyncio
import json
import time
from pathlib import Path

import main
from fastapi.testclient import TestClient


def recv(ws, aid: str | None = None) -> dict:
    """收一則投影指令。帶 aid 就跳過【其他人】的指令再回傳。

    握手之後生活迴圈就開始跑，會不定時對閒著的人發隨機走位——那對「工作投影」的斷言是雜訊。
    先前每則都嚴格比對「下一則訊息」，測試一慢就撞上去，變成間歇性失敗（實際踩到：掏手機
    那段改成等抵達之後才擺，多了往返，五次會紅兩次）。間歇性失敗比穩定失敗更糟：它會訓練
    人忽略紅燈。"""
    if aid is None:
        return json.loads(ws.receive_text())
    for _ in range(40):
        cmd = json.loads(ws.receive_text())
        if cmd.get("agent_id") == aid:
            return cmd
    raise AssertionError(f"等不到 {aid} 的指令（都是別人的）")


def post(c, **ev) -> dict:
    return c.post("/office/event", json=ev).json()


async def _no_life(a, tools):  # 生活迴圈替身：測試只看投影指令
    pass


class _FakeResp:
    status_code, text = 202, ""


class _FakeHTTP:  # cogito 入口替身：dispatch 只驗辦公室投影，不真的送任務
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeResp()


def _fake_client(**kw):
    return _FakeHTTP()


def run() -> None:
    main.client = None
    main.agent_loop = _no_life
    main.COGITO_HTTP = ""  # 測試不真連 cogito 入口（.env 可能有設）
    # 隔離持久化：不碰真的 office_state.json（否則測試互相汙染，也會弄髒開發環境）
    main.STATE_FILE = Path(main.__file__).parent / "office_state_test.json"
    main.STATE_FILE.unlink(missing_ok=True)
    main.BUBBLE_GAP = 0.01     # 測試不等真實泡泡節奏
    main.WATCH_TICK = 0.2      # watchdog 巡快一點
    main.WORK_TIMEOUT = 1e9    # 主流程不觸發失聯（最後一段才調小）
    with TestClient(main.app) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({
                "type": "waypoints",
                "agents": ["p17", "p01", "p07"],
                "list": ["chair_1", "chair_2", "chair_3", "cooler_1"],
            }))
            for _ in range(50):  # 等握手處理完
                if main.agents:
                    break
                time.sleep(0.1)
            # 名冊以 personas/*.yaml 為準（握手帶的 agents 僅供對帳），人數不寫死——
            # 加一位員工不該讓合約測試變紅；這裡測的是投影行為，不是編制大小。
            assert set(main.agents) >= {"p01", "p07", "p17"}, "握手後 agents 未建立"

            # （未知 id 不再拒收——那是頻道派工的入口，見文末；拒收只剩「員工派完」）
            # start：走工位 + 任務泡 + 掛起
            assert post(c, agent="p17", kind="start", label="盤點 repo 的 TODO",
                        detail="/w/channels/office_p17")["ok"]  # start 的 detail＝工作目錄
            assert recv(ws) == {"agent_id": "p17", "action": "move_to", "target": "chair_1"}
            assert recv(ws)["text"] == "▶ 開工"
            assert "p17" in main.busy and main.occupied["p17"] == "chair_1"

            # 防呆：工作中不收新任務；approve/reject 豁免（往下走到 COGITO_HTTP 檢查）
            r = c.post("/office/dispatch", json={"agent": "p17", "text": "再派一件"}).json()
            assert r["ok"] is False and "工作中" in r["error"]
            r = c.post("/office/dispatch", json={"agent": "p17", "text": "approve"}).json()
            assert r["ok"] is False and "COGITO_HTTP" in r["error"]
            # /stop 同樣豁免——擋住中止等於沒有中止（工作中才需要它）
            r = c.post("/office/dispatch", json={"agent": "p17", "text": "/stop"}).json()
            assert r["ok"] is False and "COGITO_HTTP" in r["error"]

            # tool → ▸ 泡；think/turn/result 不投影（靠順序驗證：夾在中間不該出現）
            post(c, agent="p17", kind="think", label="")
            post(c, agent="p17", kind="tool", label="bash", detail="grep -rn TODO")
            assert recv(ws)["text"] == "● bash"

            # 沒開委派卡的子 agent 前綴 → 退回主 agent 帶小名
            post(c, agent="p17", kind="tool", label="[Subagent:神秘人] read_file")
            assert recv(ws)["text"] == "● read_file"

            # error → ✗；msg → 內容泡
            post(c, agent="p17", kind="result", label="bash")  # 不投影
            post(c, agent="p17", kind="error", label="bash")
            assert recv(ws)["text"] == "⚠ bash"
            post(c, agent="p17", kind="msg", label="TODO 共 3 處，已列清單")
            assert recv(ws)["text"] == "→ 回報"

            # 委派上工：spawn code-reviewer → 小美（p01）起身入座 + 雙泡 + 掛起
            post(c, agent="p17", kind="tool", label="spawn_subagent:code-reviewer",
                 detail='{"agent_type":"code-reviewer"}')
            assert recv(ws) == {"agent_id": "p01", "action": "move_to", "target": "chair_2"}
            # 主 agent 走到對方桌邊站著看——「兩人在同一張桌子旁」是唯一看得出協作的畫面語言
            assert recv(ws) == {"agent_id": "p17", "action": "move_to", "target": "side_2"}
            pair = {(m["agent_id"], m["text"]) for m in (recv(ws), recv(ws))}
            assert pair == {("p17", "→ 交辦"), ("p01", "★ 支援中")}  # 兩條佇列並行，順序不保證
            assert "p01" in main.busy

            # 委派中的內部事件 → 泡泡掛到小美頭上（前綴剝掉）
            post(c, agent="p17", kind="tool", label="[Subagent:code-reviewer] read_file")
            m = recv(ws)
            assert (m["agent_id"], m["text"]) == ("p01", "● read_file")

            # 委派收工：回報泡 + 釋放
            post(c, agent="p17", kind="result", label="spawn_subagent:code-reviewer",
                 detail="LGTM，無阻塞問題")
            # 交接完【兩個人都】回位子。先前只送主 agent 回去，支援者留在被派去的那個點——
            # 規劃類的人被派到白板前（走道上），就會一直杵在那裡（實際回報：小美常卡在走道）。
            assert recv(ws, "p17") == {"agent_id": "p17", "action": "move_to", "target": "chair_1"}
            assert recv(ws, "p01") == {"agent_id": "p01", "action": "move_to", "target": "chair_2"}
            m = recv(ws, "p01")
            assert m["text"] == "✓ 回報完成", m
            assert "p01" not in main.busy
            assert any("委派" in x for x in main.agents["p01"].memory)
            r = c.get("/office/report/p01").json()  # 委派也有報告卡
            assert (r["task"], r["status"], r["report"]) == (
                "支援阿哲：code-reviewer", "ok", "LGTM，無阻塞問題")

            # 無名子 agent（探路者）：兩側正規化成空名，一樣開卡
            post(c, agent="p17", kind="tool", label="spawn_subagent")
            assert recv(ws)["agent_id"] == "p01"  # 又輪到有空的小美（move_to）
            assert recv(ws) == {"agent_id": "p17", "action": "move_to", "target": "side_2"}
            texts = {recv(ws)["text"], recv(ws)["text"]}
            assert texts == {"→ 交辦", "★ 支援中"}

            # 主任務收工：✔ 泡 + 釋放主 agent，順手收掉沒關的委派卡
            post(c, agent="p17", kind="done", label="ok")
            assert recv(ws)["text"] == "✓ 完成"
            assert "p17" not in main.busy and "p01" not in main.busy
            assert not main.sub_active
            assert not main.work_last
            assert any("接到工作任務" in x for x in main.agents["p17"].memory)
            r = c.get("/office/report/p17").json()  # 報告卡：任務 + 全文 + 狀態
            assert r["ok"] and r["name"] == "阿哲"
            assert (r["task"], r["status"], r["report"]) == (
                "盤點 repo 的 TODO", "ok", "TODO 共 3 處，已列清單")
            assert r["workdir"] == "/w/channels/office_p17"  # 產出落在哪，卡上標出來
            assert c.get("/office/report/nobody").json()["ok"] is False
            # 時間軸：逐步事件對齊 Slack 資訊量（接任務→工具→委派→收工）
            tl = [e["text"] for e in r["timeline"]]
            assert tl[0] == "📋 接到任務：盤點 repo 的 TODO"
            assert "▸ bash｜grep -rn TODO" in tl and "✓ bash" in tl
            assert "🤝 委派給 小美：code-reviewer" in tl and tl[-1] == "✔ 任務完成"
            # 委派事件在主任務串裡內嵌子卡（誰接手、做了什麼、回報什麼）
            p17 = c.get("/office/report/p17").json()
            cur = p17["history"][-1]
            sub_ev = next(e for e in cur["events"] if "委派給" in e["text"])
            assert sub_ev["text"] == "🤝 委派給 小美：code-reviewer"
            sc = sub_ev["subcard"]
            assert (sc["agent"], sc["name"], sc["status"]) == ("p01", "小美", "ok")
            assert sc["report"] == "LGTM，無阻塞問題"
            assert any("read_file" in e["text"] for e in sc["events"])

            p01 = c.get("/office/report/p01").json()
            assert [t["task"] for t in p01["history"]] == [
                "支援阿哲：code-reviewer", "支援阿哲：探路者"]  # 任務卡分組
            assert [t["status"] for t in p01["history"]] == ["ok", "lost"]  # 探路者沒回報→lost
            sub_tl = [e["text"] for t in p01["history"] for e in t["events"]]
            assert "📋 支援阿哲：code-reviewer" in sub_tl
            assert "▸ read_file" in sub_tl and "✔ 回報：LGTM，無阻塞問題" in sub_tl

            # 閒聊不當任務：短問候不開新卡、不走工位，只在現有工作串記一句對話
            cards_before = len(c.get("/office/report/p17").json()["history"])
            post(c, agent="p17", kind="start", label="nice job")
            # 有人在跟他講話：轉頭面向鏡頭（原本是背對坐著）——但不開卡、不走工位
            assert recv(ws) == {"agent_id": "p17", "action": "use", "target": "face_down"}
            post(c, agent="p17", kind="msg", label="謝謝，有需要再找我。")
            assert recv(ws)["text"] == "→ 回報"      # 只有回話泡，沒有「開工」也沒有 move_to
            post(c, agent="p17", kind="done", label="ok")
            # 聊完轉回去坐著（move_to 會清掉轉頭姿勢，回到 sit_up）
            assert recv(ws) == {"agent_id": "p17", "action": "move_to", "target": "chair_1"}
            r = c.get("/office/report/p17").json()
            assert len(r["history"]) == cards_before  # 沒開新卡
            assert r["status"] == "ok" and r["task"] == "盤點 repo 的 TODO"  # 舊卡狀態沒被動到
            assert r["report"] == "TODO 共 3 處，已列清單"  # 報告全文沒被閒聊覆蓋
            tl = [e["text"] for e in r["timeline"]]
            assert "💬 老闆：nice job" in tl and "謝謝，有需要再找我。" in tl
            assert "p17" not in main.busy

            # 失聯保險：上工後 claw-cli 死掉（不發 done）→ watchdog 逾時釋放
            post(c, agent="p17", kind="start", label="會斷線的任務")
            assert recv(ws)["action"] == "move_to"
            assert recv(ws)["text"] == "▶ 開工"
            main.WORK_TIMEOUT = 0.3  # 這時才開始算失聯
            assert recv(ws)["text"] == "⚠ 失聯沒回應"  # watchdog 巡到後自動冒泡
            assert "p17" not in main.busy and not main.work_last
            r = c.get("/office/report/p17").json()
            assert (r["task"], r["status"]) == ("會斷線的任務", "lost")
            main.WORK_TIMEOUT = 1e9  # 後面的頻道派工測試不要被失聯保險攪局

            # Slack 頻道派工：未知 id 黏性指派閒置 NPC（名冊字母序，p01 優先）
            post(c, agent="slack:C999", kind="start", label="整理週報")
            assert recv(ws) == {"agent_id": "p01", "action": "move_to", "target": "chair_2"}
            assert recv(ws)["text"] == "▶ 開工"
            assert main.conv_npc == {"slack:C999": "p01"}

            # 第二個頻道同時上工 → 指派下一位閒置員工（是誰隨編制而變，只驗「不是同一人」）
            post(c, agent="slack:C888", kind="start", label="另一頻道任務")
            second = recv(ws)["agent_id"]  # move_to
            assert recv(ws)["text"] == "▶ 開工"
            assert second != "p01" and main.conv_npc["slack:C888"] == second

            post(c, agent="slack:C999", kind="done", label="ok")
            m = recv(ws)
            assert (m["agent_id"], m["text"]) == ("p01", "✓ 完成")
            # 黏性：同頻道下一個事件仍是同一位員工
            post(c, agent="slack:C999", kind="msg", label="補充一下週報格式")
            m = recv(ws)
            assert (m["agent_id"], m["text"]) == ("p01", "→ 回報")

            # 員工派完就拒收（任務照跑，只是辦公室演不了）。開新頻道直到分不出人為止——
            # 迴圈而非寫死人數，加減員工不會讓這條合約失效。
            for n in range(1, 20):
                if not post(c, agent=f"slack:D{n}", kind="start", label="填滿名冊")["ok"]:
                    break
                assert recv(ws)["action"] == "move_to"
                assert recv(ws)["text"] == "▶ 開工"
            else:
                raise AssertionError("名冊填不滿：頻道指派沒有在人派完時停手")

            # office 平台（Web 派工）：conv=office:pXX 直接指名員工，不走動態指派
            post(c, agent="office:p07", kind="msg", label="週報整理好了")
            assert recv(ws)["text"] == "→ 回報"

            # /office/chat：進度類濾掉、審批卡設 pending + 泡泡、一般訊息進時間軸
            assert c.post("/office/chat", json={"agent": "office:p07",
                          "text": "🛠️ *正在執行工具*：`bash`"}).json()["ok"]
            appr = "⚠️ *高危操作審批請求*\nAgent 試圖執行：\n• 工具: `bash`\n任務 ID: `T1`"
            c.post("/office/chat", json={"agent": "office:p07", "text": appr})
            # 「門口有人」的判斷要看【現在誰在等審批】，不是查 occupied——那張表從不釋放，
            # 只要有人曾經走到門口再也沒移動過，後面的人就永遠被幽靈擋住（實際踩到）。
            main.occupied["p19"] = main.BOSS_DOOR   # 老徐上次走到門口就沒再動過，但他沒在等審批
            # HITL 投影：走到老闆房門口站著等
            assert recv(ws, "p07") == {"agent_id": "p07", "action": "move_to", "target": "boss_1"}
            # 姿勢要等【真的走到】才擺——move_to 會清掉姿勢，走路途中送等於沒送。
            # 這個測試原本緊接著就斷言 phone，等於把 bug 寫死成規格：實機上那個動作從來沒出現過。
            ws.send_json({"type": "arrived", "agent_id": "p07", "at": "boss_1"})
            # 等審批＝球在別人手上：掏手機，不是站著像雕像（站著不動＝閒置，兩者不能同形）。
            # 姿勢與泡泡【誰先到不保證】：泡泡走節流佇列、姿勢走等抵達的背景任務，兩條路徑
            # 沒有順序關係。先前照順序斷言，五次會紅兩次——那是測試在假設一個不存在的契約。
            got = [recv(ws, "p07") for _ in range(2)]
            assert {"agent_id": "p07", "action": "use", "target": "phone"} in got, got
            assert any(c.get("text") == "⚠ 等待審批" for c in got), got
            assert main.occupied["p07"] == "boss_1"
            assert "p07" in main.busy  # 等審批＝工作中，生活迴圈不得插隊蓋掉罰站走位
            r = c.get("/office/report/p07").json()
            assert r["approval"].startswith("⚠️ *高危操作審批請求*")
            # 名冊要看得出「誰在等你決定」（提示音之外的視覺線索）
            assert c.get("/agents").json()["p07"]["approval"] is True
            tl = [e["text"] for e in r["timeline"]]
            assert not any("正在執行工具" in x for x in tl)  # 進度類已濾
            assert any(x.startswith("💬 ⚠️") for x in tl)

            # 老闆核准 → 冒放行泡＋走回工位（cogito 入口用假的，只驗投影）
            main.httpx.AsyncClient = _fake_client
            main.COGITO_HTTP = "http://fake"
            assert c.post("/office/dispatch", json={"agent": "p07", "text": "approve"}).json()["ok"]
            got = {json.dumps(recv(ws), ensure_ascii=False, sort_keys=True) for _ in range(2)}
            assert got == {
                json.dumps({"agent_id": "p07", "action": "move_to", "target": "chair_3"},
                           ensure_ascii=False, sort_keys=True),
                json.dumps(main.say("p07", "✓ 放行"), ensure_ascii=False, sort_keys=True)}
            assert "p07" not in main.pending_approval and main.occupied["p07"] == "chair_3"

            # 審批逾時後工作恢復：人還在老闆房門口 → 看到工具事件自己回工位
            main.occupied["p07"] = "boss_1"
            post(c, agent="office:p07", kind="tool", label="bash")
            assert recv(ws) == {"agent_id": "p07", "action": "move_to", "target": "chair_3"}
            assert recv(ws)["text"] == "● bash"
            main.COGITO_HTTP = ""
            # done 收掉殘留審批卡
            post(c, agent="office:p07", kind="done", label="error", detail="審批逾時")
            recv(ws)  # ✗ 任務中斷 泡
            assert c.get("/office/report/p07").json()["approval"] == ""

            # dispatch：未設 COGITO_HTTP → 明確報錯不轉發
            r = c.post("/office/dispatch", json={"agent": "p07", "text": "x"}).json()
            assert r["ok"] is False and "COGITO_HTTP" in r["error"]

            # 鏈路健康度：canvas＝連著的畫面數、生活迴圈跑著＝live
            s = c.get("/office/status").json()
            assert s["canvas"] == 1 and s["live"] is True and s["stale"] is False

            # 多觀眾：第二個畫面加入不重啟世界，指令廣播給兩邊
            with c.websocket_connect("/ws") as ws2:
                ws2.send_text(json.dumps({"type": "waypoints", "agents": ["p01"],
                                          "list": main.waypoint_list}))
                for _ in range(50):
                    if c.get("/office/status").json()["canvas"] == 2:
                        break
                    time.sleep(0.05)
                assert c.get("/office/status").json()["canvas"] == 2
                post(c, agent="p07", kind="msg", label="兩個畫面都該收到")
                assert recv(ws)["text"] == "→ 回報"
                assert recv(ws2)["text"] == "→ 回報"
            for _ in range(50):  # ws2 關閉：只掉一個觀眾，世界照跑
                if c.get("/office/status").json()["canvas"] == 1:
                    break
                time.sleep(0.05)
            s = c.get("/office/status").json()
            assert s["canvas"] == 1 and s["live"] is True

            # SSE 失效通知：log_ev 推 agent、busy 變化推 roster
            q = asyncio.Queue()
            main.subscribers.add(q)
            main.log_ev("p17", "測試事件")
            assert q.get_nowait() == {"type": "agent", "id": "p17"}
            main.release_work("p17")
            assert q.get_nowait() == {"type": "roster", "id": ""}
            main.subscribers.discard(q)

    # 持久化 roundtrip：存檔 → 清空記憶體 → 載回，任務卡/黏性指派/審批卡都回來
    try:
        main.pending_approval["p01"] = "⚠️ 待審批"
        main.save_state()
        snapshot = {a: [dict(t) for t in cards] for a, cards in main.history.items()}
        conv_snapshot = dict(main.conv_npc)
        main.history.clear(); main.last_report.clear()
        main.conv_npc.clear(); main.pending_approval.clear()
        main.busy.clear(); main.work_last.clear()
        main.load_state()
        assert {a: [dict(t) for t in cards] for a, cards in main.history.items()} == snapshot
        assert main.conv_npc == conv_snapshot
        assert main.pending_approval == {"p01": "⚠️ 待審批"}
        # working 狀態的卡 → 復原 busy + watchdog 計時（重啟時任務可能還在跑）
        working = [a for a, card in main.last_report.items() if card["status"] == "working"]
        assert set(working) <= main.busy and set(working) <= set(main.work_last)
    finally:
        main.STATE_FILE.unlink(missing_ok=True)

    souls()
    previews()
    alerts()
    clear_day()
    parallel_subs()
    clear_all()
    note_not_echoed()
    stop_clears_approval()
    dup_msg()
    sub_by_name()
    kanban()
    meeting_progress()
    board()
    sub_release_fallback()
    turn_in_stream()
    print("✓ office 投影合約測試全過（含子 agent 映射、節流、失聯保險、子 agent 兜底釋放、"
          "回合寫入工作串、頻道派工、人設同步、提示音、清除歷史）")


def sub_release_fallback() -> None:
    """子 agent 的釋放事件掉了 → sweep_work 按徵用時間強制放人、送回座位。

    為何需要這條：主 agent 還活著時 work_last 一直在刷新，原本的失聯規則（WORK_TIMEOUT）
    永遠不會觸發，於是那個被徵用的 NPC 永遠卡在 busy、永遠不回座位。實際症狀就是
    「跑完一輪之後很多人杵著不動」。cogito 端已把釋放事件改成不可丟，這是橋端的第二道保險
    （橋自己重啟會失憶，所以兩邊都要有）。"""
    with TestClient(main.app) as c, c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "waypoints", "agents": [], "list": main.waypoint_list or ["chair_1"]}))
        npc = "p05"
        main.busy.add(npc)
        main.sub_active[("p17", "planner")] = [npc]
        main.sub_since[npc] = time.monotonic() - main.SUB_TIMEOUT - 1   # 徵用很久了，釋放沒來
        main.report_card(npc, "支援：planner")

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(main.sweep_work())             if False else asyncio.run(main.sweep_work())

        assert npc not in main.busy, "逾時未釋放：NPC 還卡在 busy，畫面上就是一直杵著"
        assert npc not in main.sub_since, "徵用時間表沒清乾淨，下一輪會重複觸發"
        assert ("p17", "planner") not in main.sub_active, "sub_active 沒清，之後的釋放會配到空佇列"
        assert main.last_report[npc]["status"] == "lost", "卡片沒收，看板會永遠顯示進行中"


def turn_in_stream() -> None:
    """回合標記【只在真的安靜時】才進工作串，而且不冒泡。

    它的用途是填補長考的空白——兩次工具呼叫之間全空白，看起來像 agent 掛了。
    但每輪都印就變成另一種噪音：一連串「第 N 輪」夾在有內容的事件中間，而「第幾輪」
    對使用者毫無意義（實際回報：「不理解所謂的第幾輪是有什麼意義」）。"""
    with TestClient(main.app) as c, c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "waypoints", "agents": [], "list": main.waypoint_list or ["chair_1"]}))
        aid = "p17"
        main.chat_mode.discard(aid)
        c.post("/office/event", json={"agent": aid, "kind": "start", "label": "寫規格", "detail": "/w"})

        # 剛剛才有事件 → 這一輪不安靜，不該留下痕跡
        before = len(main.last_report[aid]["events"])
        c.post("/office/event", json={"agent": aid, "kind": "turn", "label": "7", "detail": ""})
        assert len(main.last_report[aid]["events"]) == before, "有內容的回合不該再插一行「第 N 輪」"

        # 假裝安靜了很久 → 那段空白是真的，要標出來
        main.last_ev_at[aid] = time.monotonic() - main.TURN_QUIET - 1
        c.post("/office/event", json={"agent": aid, "kind": "turn", "label": "8", "detail": ""})
        evs = main.last_report[aid]["events"]
        assert len(evs) == before + 1, "長考的空白沒有被標出來"
        assert "第 8 輪" in evs[-1]["text"] and "思考中" in evs[-1]["text"], f"措辭不對：{evs[-1]}"


def clear_day() -> None:
    """清除某一天的歷史：只刪那一天、【不刪進行中的卡】、指標跟著移動。
    這是整個看板唯一會刪資料的端點，每一條都值得釘住。"""
    main.history.clear(); main.last_report.clear()
    a, b = main.report_card("p01", "前天做完的", ""), main.report_card("p01", "前天也做完的", "")
    a["day"] = b["day"] = "2026-01-01"
    a["status"] = b["status"] = "ok"
    stuck = main.report_card("p01", "前天沒收工的", "")   # 同一天但還在跑
    stuck["day"] = "2026-01-01"
    today = main.report_card("p01", "今天的", "")
    old = main.report_card("p01", "沒有日期的舊卡", "")
    old.pop("day"); old["status"] = "ok"

    with TestClient(main.app) as c:
        r = c.delete("/office/history/p01", params={"day": "2026-01-01"}).json()
        assert r["ok"] and r["removed"] == 2 and r["skipped"] == 1, r
        tasks = [t["task"] for t in main.history["p01"]]
        assert tasks == ["前天沒收工的", "今天的", "沒有日期的舊卡"], tasks
        assert main.last_report["p01"]["task"] == "沒有日期的舊卡"   # 指標跟著移到最後一張

        r = c.delete("/office/history/p01", params={"day": ""}).json()   # 空 day＝舊卡那組
        assert r["ok"] and r["removed"] == 1
        assert [t["task"] for t in main.history["p01"]] == ["前天沒收工的", "今天的"]

        r = c.delete("/office/history/p01", params={"day": "2026-01-01"}).json()
        assert r["ok"] is False and "進行中" in r["error"], r   # 只剩進行中的那張：不刪
        assert c.delete("/office/history/p99", params={"day": ""}).json()["ok"] is False
    main.history.clear(); main.last_report.clear()
    main.STATE_FILE.unlink(missing_ok=True)   # TestClient 收工會存檔，別留下測試殘骸


def alerts() -> None:
    """提示音事件：使用者不會盯著畫面，所以【該響的三件事】要進 SSE。
    刻意只有三種——每個工具呼叫都叮一聲等於沒有聲音，這條約定值得被釘住。"""
    got = []
    main.subscribers.clear()
    q: asyncio.Queue = asyncio.Queue()
    main.subscribers.add(q)
    try:
        main.notify("roster", "p01", alert="approval")
        main.notify("agent", "p01", alert="done")
        main.notify("agent", "p01", alert="error")
        main.notify("roster", "p01")            # 一般狀態變更：不該帶 alert
        while not q.empty():
            got.append(q.get_nowait())
    finally:
        main.subscribers.discard(q)
    assert [e.get("alert") for e in got] == ["approval", "done", "error", None], got
    assert all(e["type"] in ("roster", "agent") for e in got)


def previews() -> None:
    """產出預覽的三道界線：路徑不得越界、副檔名白名單、卡片外的目錄一律拒絕。
    這是唯一會把磁碟內容送出去的端點，破了就是任意檔案讀取。"""
    import base64, tempfile
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp) / "job"
        wd.mkdir()
        (wd / "a.md").write_text("# hi\n", encoding="utf-8")
        (wd / "k.env").write_text("TOKEN=x\n", encoding="utf-8")   # 不在白名單
        (wd / "AGENTS.md").write_text("人設\n", encoding="utf-8")   # 橋自己同步的基礎設施，不是產出
        (wd / "i.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
        (Path(tmp) / "outside.txt").write_text("secret\n", encoding="utf-8")

        main.history.clear(); main.last_report.clear()
        card = main.report_card("p01", "預覽測試", str(wd))
        cid = card["id"]
        with TestClient(main.app) as c:
            names = [f["name"] for f in c.get(f"/office/files/p01/{cid}").json()["files"]]
            assert names == ["a.md", "i.png"], f"白名單外的檔案被列出來了：{names}"
            assert c.get(f"/office/file/p01/{cid}", params={"p": "i.png"}).headers["content-type"] == "image/png"
            for bad in ("../outside.txt", "/etc/passwd", "job/../../outside.txt"):
                r = c.get(f"/office/file/p01/{cid}", params={"p": bad}).json()
                assert r["ok"] is False and "越界" in r["error"], f"路徑越界沒擋住：{bad}"
            assert c.get(f"/office/file/p01/{cid}", params={"p": "k.env"}).json()["ok"] is False

            # HTML：預設純文字（不會在橋的來源上執行）；render=1 才是 text/html，且必帶 CSP sandbox
            (wd / "page.html").write_text("<script>document.title='x'</script>", encoding="utf-8")
            plain = c.get(f"/office/file/p01/{cid}", params={"p": "page.html"})
            assert plain.headers["content-type"].startswith("text/plain")
            assert "content-security-policy" not in plain.headers
            shown = c.get(f"/office/file/p01/{cid}", params={"p": "page.html", "render": 1})
            assert shown.headers["content-type"].startswith("text/html")
            assert shown.headers["content-security-policy"] == "sandbox allow-scripts"
            # render=1 不能拿來把別種檔案變成 HTML
            assert c.get(f"/office/file/p01/{cid}", params={"p": "a.md", "render": 1}
                         ).headers["content-type"].startswith("text/plain")
            assert c.get(f"/office/files/p01/{cid + 999}").json()["ok"] is False

            # 工作區瀏覽（可進子目錄）：同一套界線，入口不同
            (wd / "sub").mkdir()
            (wd / "sub" / "note.txt").write_text("x\n", encoding="utf-8")
            (wd / "sub" / "AGENTS.md").write_text("這份是 agent 自己寫的\n", encoding="utf-8")
            root = c.get("/office/ws/p01").json()
            assert root["ok"] and root["up"] is None
            names = [(e["name"], e["dir"]) for e in root["entries"]]
            assert ("sub", True) in names and ("a.md", False) in names
            # 根目錄的 AGENTS.md 是橋同步進去的人設，不是產出——工作區面板要回答
            # 「這次做出了什麼」，混進基礎設施就是雜訊
            assert ("AGENTS.md", False) not in names, f"根目錄的 AGENTS.md 該被濾掉：{names}"
            assert ("k.env", False) in names, "白名單外的檔案要列出來（只是不給預覽）"
            assert next(e for e in root["entries"] if e["name"] == "k.env")["kind"] == "raw"
            deep = c.get("/office/ws/p01", params={"p": "sub"}).json()
            subnames = [e["name"] for e in deep["entries"]]
            assert deep["ok"] and deep["up"] == "" and "note.txt" in subnames
            # 子目錄裡的同名檔是 agent 自己寫的，屬於產出——只濾根目錄那一份
            assert "AGENTS.md" in subnames, f"子目錄的 AGENTS.md 不該被濾：{subnames}"
            for bad in ("..", "../..", "/etc"):
                assert c.get("/office/ws/p01", params={"p": bad}).json()["ok"] is False, bad
            for bad in ("../outside.txt", "/etc/passwd"):
                assert c.get("/office/wsfile/p01", params={"p": bad}).json()["ok"] is False, bad
            assert c.get("/office/wsfile/p01", params={"p": "sub/note.txt"}
                         ).headers["content-type"].startswith("text/plain")
    main.history.clear(); main.last_report.clear()


def souls() -> None:
    """人設同步的覆寫保護：手寫的 AGENTS.md 一個字都不能被動到。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        main.CHANNELS_DIR = Path(tmp)
        aid = next(a for a in main.agents if (Path(main.__file__).parent / "personas" / f"{a}.md").exists())
        dst = Path(tmp) / f"office_{aid}" / "AGENTS.md"

        assert main.sync_souls()["wrote"] >= 1          # 第一次：建檔
        assert dst.read_text(encoding="utf-8").startswith(main.SOUL_MARK)
        assert main.agents[aid].persona["name"] in dst.read_text(encoding="utf-8")

        n = main.sync_souls()                            # 第二次：內容相同就不重寫
        assert n["wrote"] == 0 and n["same"] >= 1

        mine = "# 我自己寫的專案指南\n不要動我。\n"        # 沒有標記＝人寫的
        dst.write_text(mine, encoding="utf-8")
        n = main.sync_souls()
        assert dst.read_text(encoding="utf-8") == mine, "手寫的 AGENTS.md 被覆蓋了"
        assert n["skipped"] >= 1

        dst.unlink()                                     # 刪掉標記檔 → 下次照樣補回來
        assert main.sync_souls()["wrote"] >= 1
    main.CHANNELS_DIR = None                             # 沒設定就整個不啟用
    assert main.sync_souls() == {"wrote": 0, "same": 0, "skipped": 0}


def parallel_subs() -> None:
    """並行派多個【同名】子 agent：每一個都要被釋放。
    原本 sub_active 存單一 NPC，後派的覆蓋先派的，先派的那些連 release_work 都找不到——
    會議跑完四個人永遠停在「工作中」。"""
    with TestClient(main.app) as c:
        main.busy.clear()
        main.sub_active.clear()
        parent = "p01"
        post(c, agent=parent, kind="start", label="開會")

        # 三個【未具名】子 agent（name 都正規化成 ""）同時上工
        for _ in range(3):
            post(c, agent=parent, kind="tool", label="spawn_subagent")
        helpers = [n for lst in main.sub_active.values() for n in lst]
        assert len(helpers) == 3, f"三個並行委派應各佔一位 NPC，got {helpers}"
        assert len(set(helpers)) == 3, f"同一個人被派了兩次：{helpers}"
        assert all(h in main.busy for h in helpers)

        for _ in range(3):   # 三個都收工
            post(c, agent=parent, kind="result", label="spawn_subagent")
        assert not any(h in main.busy for h in helpers), \
            f"收工後還卡在工作中：{[h for h in helpers if h in main.busy]}"
        assert not main.sub_active, f"映射沒清乾淨：{main.sub_active}"

        # 只收回兩個就收工：release_work 要把剩下那個也釋放，不能留永久 working
        for _ in range(3):
            post(c, agent=parent, kind="tool", label="spawn_subagent")
        left = [n for lst in main.sub_active.values() for n in lst]
        post(c, agent=parent, kind="result", label="spawn_subagent")
        post(c, agent=parent, kind="done", label="ok")
        assert not any(h in main.busy for h in left), \
            f"主 agent 收工後仍有沒被釋放的支援者：{[h for h in left if h in main.busy]}"


def clear_all() -> None:
    """一次清掉某位的全部工作紀錄（測試跑久了一天一天刪太瑣碎），但進行中的照樣保留。
    另外板子是【封存】不是刪除——那是一次協作的完整紀錄。"""
    import tempfile
    with TestClient(main.app) as c:
        # 卡要在 TestClient 啟動【之後】才建：startup 會跑 load_state()，把 history 整個換掉
        main.history.clear(); main.last_report.clear()
        for day in ("2026-01-01", "2026-02-02", ""):
            card = main.report_card("p01", f"{day} 的討論", "")
            card["day"], card["status"] = day, "ok"
        live = main.report_card("p01", "還在跑的", "")
        live["day"], live["status"] = "2026-03-03", "working"
        r = c.request("DELETE", "/office/history/p01", params={"scope": "all"}).json()
        assert r["ok"] and r["removed"] == 3 and r["skipped"] == 1, r
        left = [t["task"] for t in main.history["p01"]]
        assert left == ["還在跑的"], f"進行中的卡被刪了：{left}"

        # 板子：改名封存，原檔不再存在但內容還在
        with tempfile.TemporaryDirectory() as tmp:
            main.CHANNELS_DIR = Path(tmp)
            wd = Path(tmp) / f"office_{main.KANBAN}"
            wd.mkdir(parents=True)
            (wd / "board.json").write_text('{"task":"x","tasks":[]}', encoding="utf-8")
            r = c.request("DELETE", "/office/board").json()
            assert r["ok"], r
            assert not (wd / "board.json").exists(), "原檔應該被改名"
            arch = list(wd.glob("board.*.json"))
            assert len(arch) == 1 and "x" in arch[0].read_text(encoding="utf-8"), "封存檔不見了"
            assert c.request("DELETE", "/office/board").json()["ok"] is False, "沒有板子時要講清楚"
    main.CHANNELS_DIR = None


def note_not_echoed() -> None:
    """派工那行與任務卡標題是同一句話時，不要並排記兩次。
    卡片標題本來就是老闆說的那句；兩行擺一起只是同一段文字說兩次。"""
    with TestClient(main.app) as c:
        task = "用 orchestrate 模式做一份工具權限政策的稽核報告"
        main.pending_note["p07"] = main.NOTE_MARK + task
        post(c, agent="p07", kind="start", label=task)
        evs = [e["text"] for e in c.get("/office/report/p07").json()["timeline"]]
        assert sum(1 for t in evs if task[:20] in t) == 1, f"同一句話記了兩次：{evs}"
        post(c, agent="p07", kind="done", label="ok")

        # 但續跑時卡名被換掉（🔄 續跑：…），老闆原話就是新資訊，要留著
        main.pending_note["p07"] = main.NOTE_MARK + "請接續昨天那個稽核"
        post(c, agent="p07", kind="start", label="🔄 續跑：稽核報告")
        evs = [e["text"] for e in c.get("/office/report/p07").json()["timeline"]]
        assert any("請接續昨天那個稽核" in t for t in evs), "卡名不同時老闆原話要留著"
        post(c, agent="p07", kind="done", label="ok")


def stop_clears_approval() -> None:
    """按中止時若正卡在審批：審批卡要當場收掉，而且要先送駁回。
    agent 這時阻塞在等審批，中止它讀不到——不先解開就會停在「按了中止卻還要你選」。"""
    sent = []

    class _Rec(_FakeHTTP):
        async def post(self, url, **kw):
            sent.append(kw.get("json", {}).get("text"))
            return _FakeResp()

    main.COGITO_HTTP = "http://fake"
    old = main.httpx.AsyncClient
    main.httpx.AsyncClient = lambda **kw: _Rec()
    try:
        with TestClient(main.app) as c:
            main.pending_approval["p07"] = "⚠️ 高危操作審批請求：rm -rf"
            main.busy.add("p07")
            r = c.post("/office/dispatch", json={"agent": "p07", "text": "/stop"}).json()
            assert r["ok"], r
            assert sent == ["reject", "/stop"], f"要先駁回再中止，實際送出：{sent}"
            assert "p07" not in main.pending_approval, "審批卡沒收掉，畫面會一直卡在選擇上"
    finally:
        main.httpx.AsyncClient = old
        main.COGITO_HTTP = ""
        main.busy.discard("p07")


def dup_msg() -> None:
    """同一則助理訊息走兩條路送來（/office/event kind=msg 與 /office/chat），只該記一次。
    兩條路的截斷長度不同（200 vs 300），所以是「內容像但不完全一樣」的雙胞胎，最難察覺。"""
    with TestClient(main.app) as c:
        main.busy.discard("p05")
        post(c, agent="p05", kind="start", label="寫報告")
        long = "落地驗證通過。開工前給你看板子。" + "細節" * 200
        post(c, agent="p05", kind="msg", label=long)
        c.post("/office/chat", json={"agent": "office:p05", "text": long})
        evs = [e["text"] for e in c.get("/office/report/p05").json()["timeline"]]
        bodies = [t.lstrip("💬 ").strip()[:40] for t in evs]
        assert bodies.count(long[:40]) == 1, f"同一則訊息被記了 {bodies.count(long[:40])} 次：{evs}"

        # 反方向也要擋：chat 先到、msg 後到（誰先到不保證，只擋一邊等於沒擋）
        long2 = "另一段夠長的助理訊息開頭" + "內容" * 200
        c.post("/office/chat", json={"agent": "office:p05", "text": long2})
        post(c, agent="p05", kind="msg", label=long2)
        evs = [e["text"] for e in c.get("/office/report/p05").json()["timeline"]]
        bodies = [t.lstrip("💬 ").strip()[:40] for t in evs]
        assert bodies.count(long2[:40]) == 1, f"chat 先到時沒擋住：{bodies.count(long2[:40])} 次"

        # 短記號本來就會重複出現（✓ bash 一天到晚有），不能被去重吃掉
        for _ in range(2):
            post(c, agent="p05", kind="error", label="bash")
        evs = [e["text"] for e in c.get("/office/report/p05").json()["timeline"]]
        assert sum(1 for t in evs if t.startswith("✗ bash")) == 2, "短記號不該被當成重複"

        # 【排版不同】的雙胞胎也要擋：cogito 會針對平台改寫排版，同一段話 chat 那條送
        # 「**粗體**＋```區塊」、event 那條送「## 標題＋markdown 表格」，實測在第 44 個字就
        # 分岔。比文字前綴永遠比不出來——「兩邊文字相同」這個假設從頭就是錯的。
        # 共同開頭要夠長才符合真實形狀：分岔發生在敘述之後的【結構】（表格 vs 程式碼區塊），
        # 不是開頭第一句。指紋窗口若為了遷就更早的分岔而縮短，就會開始把不同訊息誤判成同一則。
        lead = "基於目前狀態，接下來可做的 action 大致分四類，按「該不該做／成本」排序給你："
        chat_fmt = lead + "\n\n**🟢 收尾類（低成本，建議先做）**\n```\nA. 清理\n```"
        msg_fmt = lead + "\n\n## 🟢 收尾類（低成本，建議先做）\n| Action | 成本 |"
        post(c, agent="p05", kind="msg", label=msg_fmt)
        c.post("/office/chat", json={"agent": "office:p05", "text": chat_fmt})
        evs = [e["text"] for e in c.get("/office/report/p05").json()["timeline"]]
        assert sum(1 for t in evs if "接下來可做的" in t) == 1, f"排版不同的雙胞胎沒擋住：{evs[-3:]}"

        # 【短】訊息也要去重：實測踩到一句 38 字的雙胞胎，用長度當門檻就會漏掉。
        short = "會開完，上板。board.json 每輪會被清（老教訓），先確認再決定重建或增補。"
        post(c, agent="p05", kind="msg", label=short)
        c.post("/office/chat", json={"agent": "office:p05", "text": short})
        evs = [e["text"] for e in c.get("/office/report/p05").json()["timeline"]]
        assert sum(1 for t in evs if short[:20] in t) == 1, f"短訊息的雙胞胎沒擋住：{evs[-3:]}"

        # 但同一句話【由同一條路】說兩次，是真的說了兩次，不能吃掉
        c.post("/office/chat", json={"agent": "office:p05", "text": "再說一次這句話給你聽好嗎"})
        post(c, agent="p05", kind="tool", label="bash")
        c.post("/office/chat", json={"agent": "office:p05", "text": "再說一次這句話給你聽好嗎"})
        evs = [e["text"] for e in c.get("/office/report/p05").json()["timeline"]]
        assert sum(1 for t in evs if "再說一次這句話" in t) == 2, "同一條路的重複發言是真的，不該被吃掉"

        # 但真的又說一次（隔了幾則）要記得下來——去重不能把重複發言吃掉
        for i in range(3):
            post(c, agent="p05", kind="tool", label=f"bash{i}")
        c.post("/office/chat", json={"agent": "office:p05", "text": long})
        evs = [e["text"] for e in c.get("/office/report/p05").json()["timeline"]]
        bodies = [t.lstrip("💬 ").strip()[:40] for t in evs]
        assert bodies.count(long[:40]) == 2, "隔了幾則之後的同句話是真的又說了一次，不該被吃掉"
        post(c, agent="p05", kind="done", label="ok")


def sub_by_name() -> None:
    """派給「小美」就要由小美演。
    kanban 頻道的具名 agent 用人名，SUB_NPC 那張表收的是角色名——只查角色表的話，
    板子上寫 👤 小美、畫面走過去的卻是別人，兩邊各說各話。"""
    main.busy.clear()
    main.sub_active.clear()
    mei = "p01"
    assert main.agents[mei].name == "小美"
    assert main.pick_sub_npc("p19", "小美") == mei, "人名沒對到名冊"
    assert main.pick_sub_npc("p19", "planner") == "p01", "原本的角色表要繼續有效"
    # 本人正忙：可以換角代打，但不能因此停演
    main.busy.add(mei)
    other = main.pick_sub_npc("p19", "小美")
    assert other is not None and other != mei, "本人忙碌時該找別人代打"
    main.busy.discard(mei)
    # 派給自己不會挑到自己（那會變成一個人同時是主也是支援）
    assert main.pick_sub_npc(mei, "小美") != mei

    # 用【角色名】當 agent_type：橋這邊對得到 NPC（畫面正常），但 cogito 載不到人設。
    # 這個錯配正是那個 bug 藏最久的原因，所以要留下痕跡——判準是「有沒有人設」，
    # 不是「橋認不認得」（implementer 在 SUB_NPC 裡，橋認得，但它不是任何人的名字）。
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        who = main.pick_sub_npc("p19", "implementer")
    assert who is not None, "認不得名字也要有人代打，演出不能停"
    assert "載不到人設" in buf.getvalue(), f"角色名當 agent_type 沒留下痕跡：{buf.getvalue()!r}"

    # 正牌人名不該有警告
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        main.pick_sub_npc("p19", "小美")
    assert buf2.getvalue() == "", f"用人名不該警告：{buf2.getvalue()!r}"


def board() -> None:
    """看板投影：四欄，而「等待相依」是【算出來的】不是 agent 自己標的。
    少一個要維護的狀態，就少一種寫錯的可能——agent 只管 todo/doing/done。"""
    import tempfile
    with TestClient(main.app) as c:
        assert c.get("/office/board").json()["ok"] is False   # 沒有 board.json：面板整個不顯示

        with tempfile.TemporaryDirectory() as tmp:
            main.CHANNELS_DIR = Path(tmp)
            wd = Path(tmp) / f"office_{main.KANBAN}"
            wd.mkdir(parents=True)
            (wd / "board.json").write_text(json.dumps({
                "task": "蓋一間會議室",
                "tasks": [
                    {"id": "api", "title": "後端端點", "deps": [], "status": "done"},
                    {"id": "ui", "title": "面板", "deps": ["api"], "status": "doing", "owner": "p12"},
                    {"id": "test", "title": "驗收", "deps": ["api", "ui"], "status": "todo"},
                    {"id": "art", "title": "美術", "deps": [], "status": "todo"},
                ],
            }, ensure_ascii=False), encoding="utf-8")

            r = c.get("/office/board").json()
            assert r["ok"] and r["task"] == "蓋一間會議室"
            assert r.get("mode") != "meeting", "有板子時就該畫板子，不是會議進度"
            by = {col["key"]: col["cards"] for col in r["columns"]}
            assert [x["id"] for x in by["done"]] == ["api"]
            assert [x["id"] for x in by["doing"]] == ["ui"]
            # art 沒有相依 → 待辦；test 等 ui（還沒完成）→ 等待相依。兩者 status 都是 todo。
            assert [x["id"] for x in by["todo"]] == ["art"], "沒有相依的不該被算成等待中"
            assert [x["id"] for x in by["blocked"]] == ["test"], "相依沒完成的要進等待欄"
            assert by["blocked"][0]["deps"] == ["api", "ui"], "要說明在等誰，只標『等待中』等於沒說"
            assert by["doing"][0]["owner"] == main.agents["p12"].name, "owner 要換成看得懂的名字"

            # live：主持人沒在跑的時候，板子上的「進行中」是舊資料——畫面要講出來，不能
            # 讓人以為現在有人在做。這是投影誠實，不是裝飾。
            main.busy.discard(main.KANBAN)
            assert c.get("/office/board").json()["live"] is False
            main.busy.add(main.KANBAN)
            assert c.get("/office/board").json()["live"] is True
            main.busy.discard(main.KANBAN)
    main.CHANNELS_DIR = None


def meeting_progress() -> None:
    """還沒上板時，任務板要退回【會議進度】：誰被派了、誰回來了。

    上板前的會議是整個流程裡最久也最貴的一段，那段時間板子全空——使用者只能看著
    工作串捲，不知道還要等多久、誰還沒回。資料全部來自既有的委派紀錄，不是編一個進度條。"""
    with TestClient(main.app) as c:
        main.history.clear(); main.last_report.clear()
        main.busy.clear(); main.sub_active.clear()

        # 還沒開會：整個面板不顯示（空板子是雜訊）
        assert c.get("/office/board").json()["ok"] is False

        # 開會中但還沒派人：也沒有進度可報
        post(c, agent=main.KANBAN, kind="start", label="設計訪客導覽")
        assert c.get("/office/board").json()["ok"] is False, "還沒派人就不該報進度"

        # 派兩個、回一個
        for _ in range(2):
            post(c, agent=main.KANBAN, kind="tool", label="spawn_subagent")
        post(c, agent=main.KANBAN, kind="result", label="spawn_subagent", detail="我的意見")
        r = c.get("/office/board").json()
        assert r["ok"] and r["mode"] == "meeting", r
        assert (r["done"], r["total"]) == (1, 2), f"進度不對：{r}"
        assert sum(1 for p in r["people"] if p["done"] and p["ok"]) == 1
        assert all(p["name"] for p in r["people"]), "要有名字，不然看不出在等誰"

        post(c, agent=main.KANBAN, kind="done", label="ok")
        assert c.get("/office/board").json()["ok"] is False, "收工後就不該再報會議進度"


def kanban() -> None:
    """看板是名冊卡但不是 NPC：不能被當成空閒員工去接別人的頻道，也不該有身體。
    另外驗具名 agent 有落地——沒有它，主持人點名點到的是空氣。"""
    import tempfile

    # 1. 不進「可指派的空閒 NPC」名單：否則任何一個沒指名的頻道都可能被派給看板，
    #    那個頻道的事件就會全部演不出來（看板沒有身體）。
    assert main.KANBAN in main.agents, "看板要在名冊裡（任務卡/工作串靠它）"
    assert main.KANBAN not in main.npcs(), "看板不該被當成有身體的員工"
    main.conv_npc.clear()
    for _ in range(len(main.npcs()) + 2):        # 把所有 NPC 都佔滿，逼它去找下一個可用的
        main.resolve_npc(f"slack:{_}")
    assert main.KANBAN not in main.conv_npc.values(), "看板被當成空閒員工指派出去了"
    main.conv_npc.clear()

    # 2. 具名 agent 落地：檔名用【名字】，主持人就是照名字點名的。
    with tempfile.TemporaryDirectory() as tmp:
        main.CHANNELS_DIR = Path(tmp)
        assert main.sync_agents() >= 1
        # 【共享根】的 .claw/agents/，不是頻道目錄——cogito 的 AgentLoader 用的是
        # SkillsBaseDir=rootDir。寫錯地方的話檔案存在卻永遠載不到（實際踩到：主持人
        # 明明被守則要求用人名，卻只能退回 implementer，因為人名檔它根本看不見）。
        d = main.agents_dir()
        assert d == Path(tmp).parent / ".claw" / "agents", f"寫到了錯的目錄：{d}"
        names = {p.stem for p in d.glob("*.md")}
        assert main.agents["p19"].name in names, f"老徐沒被寫成具名 agent：{names}"
        assert main.agents[main.KANBAN].name not in names, "看板不該把自己也列成可點名的成員"

        one = next(d.glob("*.md"))
        head = one.read_text(encoding="utf-8")
        assert head.startswith("---\nname: "), "缺 frontmatter，cogito 解析不出名字"
        assert "description: " in head.split("---")[1], "缺 description——主持人就不知道何時該點他"

        # tools 必須宣告：cogito 的具名 agent 沒宣告就只拿到唯讀子集（read_file + bash），
        # 於是【實作類的工作永遠派不出去】——主持人只能自己做，板子上的 owner 全是裝飾，
        # Unity 也不會有人走動（沒有委派就沒有投影）。實測踩到整場沒有任何子 agent 出現。
        impl = (d / f'{main.agents["p12"].name}.md').read_text(encoding="utf-8")   # 小葵：前端
        assert "write_file" in impl and "edit_file" in impl, "實作型的人要動得了檔案"
        boss = (d / f'{main.agents["p19"].name}.md').read_text(encoding="utf-8")   # 老徐：CTO
        assert "write_file" not in boss, "決策型維持唯讀——他的產出是判斷不是檔案"

        assert main.sync_agents() == 0            # 內容相同不重寫
        mine = "# 我自己寫的\n不要動。\n"           # 沒有標記＝人寫的
        one.write_text(mine, encoding="utf-8")
        main.sync_agents()
        assert one.read_text(encoding="utf-8") == mine, "手寫的具名 agent 被覆蓋了"
    main.CHANNELS_DIR = None
    assert main.sync_agents() == 0                # 沒設定就整個不啟用


if __name__ == "__main__":
    run()

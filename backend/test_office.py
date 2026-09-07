"""Office 投影橋合約測試：假 Unity（TestClient WS）收指令，驗 /office/event 投影表。

跑法：.venv/bin/python test_office.py
不碰真 Unity、不叫 Claude API（生活迴圈整個 patch 掉，測試全確定性）。
"""
import asyncio
import json
import subprocess
import os
import time
from pathlib import Path

import main
from fastapi.testclient import TestClient


def recv(ws, aid: str | None = None, emote: bool = False) -> dict:
    """收一則投影指令。帶 aid 就跳過【其他人】的指令再回傳。emote=True 才收徽章指令。

    握手之後生活迴圈就開始跑，會不定時對閒著的人發隨機走位——那對「工作投影」的斷言是雜訊。
    先前每則都嚴格比對「下一則訊息」，測試一慢就撞上去，變成間歇性失敗（實際踩到：掏手機
    那段改成等抵達之後才擺，多了往返，五次會紅兩次）。間歇性失敗比穩定失敗更糟：它會訓練
    人忽略紅燈。"""
    for _ in range(60):
        cmd = json.loads(ws.receive_text())
        # 鏡頭指令（focus）不是對某個 NPC 的投影，是對【相機】下的——一律跳過。
        # 它會插在任何位置（出錯時鏡頭先過去、再冒泡），拿它去比對「下一則」必然錯。
        if cmd.get("action") == "focus":
            continue
        # 狀態徽章同理：它是背景頻道（等審批、額度、空轉），跟著狀態變化插在任何位置，
        # 拿它去比對「下一則」一樣會撞。要驗徽章的測試自己帶 emote=True。
        if cmd.get("action") == "emote" and not emote:
            continue
        if aid is None or cmd.get("agent_id") == aid:
            return cmd
        # 帶 aid 時才會走到這裡：跳過別人的指令再繼續等
    raise AssertionError(f"等不到{'指令' if aid is None else aid + ' 的指令'}（都是別人的或鏡頭指令）")


def post(c, **ev) -> dict:
    return c.post("/office/event", json=ev).json()


async def _no_life(a, tools):  # 生活迴圈替身：測試只看投影指令
    pass


class _FakeResp:
    status_code, text = 202, ""

    def __init__(self, body: dict | None = None):
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class _FakeHTTP:  # cogito 入口替身：dispatch 只驗辦公室投影，不真的送任務
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeResp()

    async def get(self, *a, **k):
        # 預設「問不到」：模型清單那條路徑的降級行為因此是【預設被驗到】的，
        # 而不是要另外寫一個測試才會走到。要驗成功路徑的測試自己覆寫這個。
        raise main.httpx.HTTPError("fake: 這個替身沒有 GET")


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
    main.GIFT_HOLD = 0         # 遞交停留是演出節奏，合約只驗指令有沒有出
    main.HURT_HOLD = 0
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
            # /stop 也豁免這道防呆，但它【不會】停在這裡回錯——中止是使用者的決定，
            # 一定收得掉（先前沒設 COGITO_HTTP 就回錯，卡片永遠開著）。整條行為在
            # stop_always_works() 驗；這裡不按它，按了會收掉卡、後面的投影斷言就沒對象了。

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
            # 出錯先閃紅（一次性動作，Unity 自己退掉），再冒 ✗ 泡。方向跟坐姿：sit_up → hurt_up
            m = recv(ws)
            assert (m.get("action"), m.get("target")) == ("use", "hurt_up"), f"出錯要先閃紅：{m}"
            assert recv(ws)["text"] == "⚠ bash"
            post(c, agent="p17", kind="msg", label="TODO 共 3 處，已列清單")
            assert recv(ws)["text"] == "→ 回報"

            # 閱讀投影：讀類工具 → 低頭看書；連續讀不重發；換非讀類 → 放下書坐回去
            post(c, agent="p17", kind="tool", label="read_file", detail="a.md")
            m = recv(ws)
            assert (m["agent_id"], m["action"], m["target"]) == ("p17", "use", "book"), m
            assert recv(ws)["text"] == "● read_file"
            post(c, agent="p17", kind="tool", label="read_file", detail="b.md")
            assert recv(ws)["text"] == "● read_file"   # 第二次讀：只有泡泡，沒有重複的姿勢指令
            post(c, agent="p17", kind="tool", label="bash", detail="make test")
            m = recv(ws)
            assert (m["action"], m["target"]) == ("use", "sit_up"), m
            assert recv(ws)["text"] == "● bash"

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

            # 委派收工：交付戲 → 回報泡 + 釋放。
            # 收件【成功】的第一個指令是支援者的遞交姿勢——主 agent 從委派起就站在她
            # 桌邊（side_2 在 chair_2 東側），她面東把成果遞出去，演完兩人才各自回位。
            post(c, agent="p17", kind="result", label="spawn_subagent:code-reviewer",
                 detail="LGTM，無阻塞問題")
            m = recv(ws, "p01")
            assert (m["action"], m["target"]) == ("use", "gift_right"), m
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
            appr = ("⚠️ *高危操作審批請求*\nAgent 試圖執行：\n• 工具: `bash`\n"
                    "• 參數: `{\"command\":\"rm -rf /tmp/x\"}`\n任務 ID: `T1`\n"
                    "👉 直接回復 `approve` / `reject` 即可。5 分鐘內無響應將自動拒絕。")
            c.post("/office/chat", json={"agent": "office:p07", "text": appr})
            # 【倒數要真的被啟動】：光有 tick_approval 沒人叫它，徽章就會停在第 0 格
            # 整整五分鐘——看起來像個靜態圖示，倒數的意義整個沒了。
            for _ in range(50):
                time.sleep(0.02)
                if "p07" in main.approval_tick:
                    break
            assert "p07" in main.approval_tick, "審批開了卻沒人推倒數"
            assert main.want_emote("p07").startswith("timer_"), main.want_emote("p07")

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
            # 結構化欄位：收卡時就拆好（工具/參數/任務 ID/逾時），外殼直接排版面不靠 markdown
            m = r["approval_meta"]
            assert m and (m["tool"], m["task_id"], m["timeout_s"]) == ("bash", "T1", 300), m
            assert m["params"] == '{"command":"rm -rf /tmp/x"}', m
            assert r["approval_left"] is not None and 0 < r["approval_left"] <= 300
            # 樣板對不上（改版、舊狀態檔）→ 解析器回 None，外殼退回原文渲染——結構化是加分不是門檻
            assert main.parse_approval("⚠️ *高危操作審批請求*\n• 工具: `bash`\n任務 ID: `T0`") is None
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
    headcount()
    board_archive()
    cost_projection()
    steer_dispatch()
    status_emote()
    proposed_memory_badge()
    approval_countdown_walks()
    roster_carries_badges()
    reject_always_works()
    cli_keeps_session()
    proposed_review()
    approver_key_separation()
    caps_refresh()
    rate_limit_wording()
    model_per_agent()
    cli_mode()
    full_stream()
    repo_binding()
    git_lens()
    schedule_jobs()
    clear_all()
    note_not_echoed()
    stop_clears_approval()
    stop_always_works()
    dup_msg()
    sub_by_name()
    kanban()
    standup_meeting()
    meeting_progress()
    board()
    camera()
    sub_release_fallback()
    hurt_projection()
    turn_in_stream()
    print("✓ office 投影合約測試全過（含子 agent 映射、節流、失聯保險、子 agent 兜底釋放、"
          "回合寫入工作串、頻道派工、人設同步、提示音、清除歷史）")


def hurt_projection() -> None:
    """出錯 → 整身閃紅一下（LimeZu hurt 列）。先前 error 只有鏡頭推過去，身體毫無反應。

    方向跟坐姿走：老徐（p19）坐 sit_left → hurt_left。支援者失敗也閃（與「成功→遞交」對稱）。
    Unity 端把它當一次性動作自己退掉，所以合約只驗「哪一下送了什麼」，不驗還原。"""
    def until(ws, aid, pred):
        for _ in range(40):
            m = recv(ws, aid)
            if pred(m):
                return m
        raise AssertionError(f"{aid} 一直沒送出預期的指令")

    with TestClient(main.app) as c, c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "waypoints", "agents": [],
                                 "list": main.waypoint_list or ["chair_1"]}))
        for _ in range(50):
            if main.agents:
                break
            time.sleep(0.1)
        for aid in ("p19", "p17", "p01"):
            main.busy.discard(aid)
        post(c, agent="p19", kind="start", label="架構評估")
        post(c, agent="p19", kind="error", label="go build")
        # 哨兵：出錯之後補一則回報，保證後面一定還有訊息——閃紅缺了是乾淨的斷言紅，不是卡死
        post(c, agent="p19", kind="msg", label="哨兵")
        m = until(ws, "p19", lambda m: m.get("action") == "use" or m.get("text") == "→ 回報")
        assert (m.get("action"), m.get("target")) == ("use", "hurt_left"), \
            f"老徐坐 sit_left，出錯該從左側閃紅：{m}"
        # 沒在上工的人出錯（殭屍事件）不閃——那不是他手上的任務
        post(c, agent="p19", kind="done", label="error")
        main.busy.discard("p19")

        # 支援者失敗：委派收件失敗 → 支援者閃紅（成功是遞交）
        post(c, agent="p17", kind="start", label="整合")
        post(c, agent="p17", kind="tool", label="spawn_subagent:code-reviewer",
             detail='{"agent_type":"code-reviewer"}')
        until(ws, "p01", lambda m: m.get("text") == "★ 支援中")
        post(c, agent="p17", kind="error", label="spawn_subagent:code-reviewer", detail="炸了")
        # 失敗後支援者一定會被叫回座位（move_to）——拿它當哨兵，閃紅要在它之前
        m = until(ws, "p01", lambda m: m.get("action") in ("use", "move_to"))
        assert (m["action"], m.get("target")) == ("use", "hurt_up"), f"支援者失敗要閃紅：{m}"
        post(c, agent="p17", kind="done", label="ok")
        for aid in ("p19", "p17", "p01"):
            main.busy.discard(aid)


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
            # 改動時間：agent 邊做邊寫檔，哪些是這次任務剛產出的、哪些是上週留下來的，
            # 光看檔名分不出來。目錄與檔案都要有——資料夾也會被寫進東西。
            import time as _t
            now = int(_t.time())
            for e in root["entries"]:
                assert isinstance(e.get("mtime"), int), f"{e['name']} 少了 mtime：{e}"
                assert abs(e["mtime"] - now) < 300, f"{e['name']} 的 mtime 不合理：{e['mtime']}"
            assert any(e["dir"] and "mtime" in e for e in root["entries"]), "資料夾也要有 mtime"

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


def headcount() -> None:
    """協作人數上限：看板專用、只加在【新任務】上、超出名冊要擋。

    這條合約的另一半在 personas/kanban.md（「開會的規矩」第 1 條）——兩邊都改到才算數，
    所以措辭關鍵字在這裡也驗一次：主持人認的是「【協作限制】」那五個字。
    """
    sent = []

    class _Cap(_FakeHTTP):
        async def post(self, url, **kw):
            sent.append(kw.get("json", {}))
            return _FakeResp()

    with TestClient(main.app) as c:
        main.httpx.AsyncClient = lambda **kw: _Cap()
        main.COGITO_HTTP = "http://fake"
        main.busy.clear()

        c.post("/office/dispatch", json={"agent": main.KANBAN, "text": "題目 A", "people": 3})
        assert "【協作限制】" in sent[-1]["text"] and "3 位" in sent[-1]["text"], sent[-1]
        assert sent[-1]["text"].startswith("題目 A"), "限制要附在題目【後面】，不然卡片標題被前綴洗掉"

        # 一位＝不開會，但落檔流程照舊（人少不等於可以跳過 spec/board）
        c.post("/office/dispatch", json={"agent": main.KANBAN, "text": "題目 B", "people": 1})
        one = sent[-1]["text"]
        assert "1 位" in one and "不必開會" in one and "board.json" in one, one

        # 自動：沒帶 people 就一個字都不加——預設行為不能被這個功能改掉
        c.post("/office/dispatch", json={"agent": main.KANBAN, "text": "題目 C"})
        assert sent[-1]["text"] == "題目 C", sent[-1]

        # 超出名冊擋下來（上限＝實際人數，不寫死）
        r = c.post("/office/dispatch", json={"agent": main.KANBAN, "text": "題目 D",
                                             "people": len(main.npcs()) + 1}).json()
        assert r["ok"] is False and "參與人數" in r["error"], r

        # 一般員工帶了 people 也不該被加料——他本來就是一個人做
        c.post("/office/dispatch", json={"agent": "p01", "text": "題目 E", "people": 2})
        assert sent[-1]["text"] == "題目 E", sent[-1]

        # 進行中的互動不加料：一句 approve 被接上一段指令就不再是 approve 了
        main.pending_approval["p01"] = "x"
        main.approval_src["p01"] = "office:p01"
        main.conv_npc.clear()
        main.busy.add(main.KANBAN)   # 中止只在有事做的時候有意義（沒事做會直接回「沒有進行中的任務」）
        c.post("/office/dispatch", json={"agent": main.KANBAN, "text": "/stop", "people": 2})
        assert sent[-1]["text"] == "/stop", sent[-1]
        main.busy.discard(main.KANBAN)
        main.stopped.discard(main.KANBAN)
        main.clear_approval("p01")
    main.COGITO_HTTP = ""


def board_archive() -> None:
    """封存板：沒有活板時也列得出來、指定看得到、而且【永遠不是 live】、檔名只認白名單。

    最後那條是安全線：f= 是照使用者輸入去讀磁碟的入口，白名單比任何字串檢查都可靠。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        k = Path(tmp) / "office_kanban"
        k.mkdir()
        (k / "board.0812-123840.json").write_text(json.dumps({
            "task": "封存的題目", "tasks": [{"id": "a", "title": "甲", "status": "done"},
                                            {"id": "b", "title": "乙", "deps": ["a"], "status": "todo"},
                                            {"id": "c", "title": "丙", "deps": ["b"], "status": "todo"}]},
            ensure_ascii=False), encoding="utf-8")
        old_dir, main.CHANNELS_DIR = main.CHANNELS_DIR, Path(tmp)
        main.history.clear(); main.last_report.clear()
        try:
            with TestClient(main.app) as c:
                # 沒有活板：面板不能整個消失，否則那些紀錄等於被鎖在磁碟上
                r = c.get("/office/board").json()
                assert r["ok"] is False, r
                assert [a["file"] for a in r["archives"]] == ["board.0812-123840.json"], r
                assert r["archives"][0]["task"] == "封存的題目" and r["archives"][0]["n"] == 3

                # 指定看：欄位照樣【算】出來，不是照 status 直接分——
                # b 的相依(a)已完成＝待辦，c 的相依(b)還沒完成＝等待相依
                r = c.get("/office/board", params={"f": "board.0812-123840.json"}).json()
                assert r["ok"] and r["archived"] == "board.0812-123840.json", r
                got = {c2["key"]: len(c2["cards"]) for c2 in r["columns"]}
                assert got == {"todo": 1, "blocked": 1, "doing": 0, "done": 1}, got

                # 封存板【永遠】不是活的——就算主持人此刻正在跑，這塊板早就停在收掉那一刻
                main.busy.add(main.KANBAN)
                assert c.get("/office/board", params={"f": "board.0812-123840.json"}
                             ).json()["live"] is False
                main.busy.discard(main.KANBAN)

                # 檔名白名單：只認清單裡的檔名。
                # ⚠ 這裡要驗【錯誤訊息】而不只是 ok=False——越界路徑就算白名單被拔掉，
                # 最後也會因為「讀不動」回 false，測試照樣綠。那種綠的是假的：
                # 真正的風險是指到一個【合法 JSON】的檔（磁碟上到處都是），
                # 那時沒有白名單就真的讀出來了。所以判準是「有沒有走到讀檔那一步」。
                for bad in ("../../../etc/passwd", "board.json", "沒這個.json"):
                    r = c.get("/office/board", params={"f": bad}).json()
                    assert r["ok"] is False and r["error"] == "找不到這塊封存板", (bad, r)
        finally:
            main.CHANNELS_DIR = old_dir


def cost_projection() -> None:
    """收工帶真實花費：卡片與時間軸都看得到錢；0/未知【什麼都不顯示】。

    誠實線：只認 cogito 報的正數（協定：0/未知不送）。畫 $0.0000 會把「沒拿到 usage」
    偽裝成「免費」——投影估計值跟畫假的進度條是同一種謊，寧可空白。
    """
    with TestClient(main.app) as c:
        post(c, agent="p05", kind="start", label="花錢的任務")
        post(c, agent="p05", kind="done", label="ok", cost=0.0231)
        r = c.get("/office/report/p05").json()
        assert r["cost"] == 0.0231, r.get("cost")
        assert any("✔ 任務完成（$0.0231）" in e["text"] for e in r["timeline"]), r["timeline"]

        # cost=0（沒拿到 usage）與負數（上游出 bug 也不能畫出 $-0.01）：
        # 卡片不帶欄位、時間軸不出現錢——不顯示、不編造。
        # ⚠ 0 靠 falsy 就擋得住，守門的 cost <= 0 真正扛的是負數——所以要驗負的。
        post(c, agent="p05", kind="start", label="不知道花多少的任務")
        post(c, agent="p05", kind="done", label="ok", cost=-0.01)
        r = c.get("/office/report/p05").json()
        assert "cost" not in r, r["cost"]
        assert not any("$" in e["text"] for e in r["timeline"]), r["timeline"]

        # 中斷也標帳——燒掉的錢不因任務失敗就不見
        post(c, agent="p05", kind="start", label="燒了錢又失敗的任務")
        post(c, agent="p05", kind="done", label="error", detail="爆了", cost=0.5)
        r = c.get("/office/report/p05").json()
        assert r["cost"] == 0.5
        assert any("✗ 任務中斷（$0.5000）" in e["text"] for e in r["timeline"]), r["timeline"]


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
            assert "p07" not in main.busy, "中止之後人就該放出來"
    finally:
        main.httpx.AsyncClient = old
        main.COGITO_HTTP = ""
        main.busy.discard("p07")


def stop_always_works() -> None:
    """中止是【使用者的意思表示】，按下去就一定結束——不管上游停不停得下來。

    先前它被寫成「對上游的請求」：CLI 沒有行程時回一句「沒有進行中的 CLI 任務」什麼都
    不做，cogito 連不上時直接回錯。兩種情況下卡片都永遠開著、人永遠 busy，唯一的出路是
    等失聯保險五分鐘或去改 state 檔（實際回報：阿海一直在工作中，中止按不動）。

    收得掉是一回事、有沒有真的叫停上游是另一回事：後者寫進工作串，不能混為一談。
    """
    def stop(c, aid):
        return c.post("/office/dispatch", json={"agent": aid, "text": "/stop"}).json()

    def card_of(c, aid):
        return c.get(f"/office/report/{aid}").json()

    with TestClient(main.app) as c:
        # ① CLI 引擎、沒有行程可砍（卡片來自手打事件或上個行程留下的）
        main.busy.discard("p05")
        main.engine_sent["p05"] = main.ENGINE_CLI
        post(c, agent="p05", kind="start", label="停不掉的任務")
        assert "p05" in main.busy
        r = stop(c, "p05")
        assert r["ok"], f"沒有 CLI 行程也必須停得掉：{r}"
        assert "p05" not in main.busy, "中止後人沒放出來"
        card = card_of(c, "p05")
        assert card["status"] == "stopped", f"卡片要收成【已中止】而不是失敗：{card['status']}"
        evs = [e["text"] for e in card["timeline"]]
        assert any("老闆中止" in t for t in evs), evs
        # 誠實：沒有行程可砍就要講出來，不能讓人以為真的叫停了什麼
        assert any("沒有進行中的 CLI 行程" in t for t in evs), evs

        # ② 中止之後才到的收尾事件不該自相矛盾（砍掉行程的退出碼 -9 是我們自己造成的）
        post(c, agent="p05", kind="done", label="error", detail="CLI 異常結束（退出碼 -9）")
        card = card_of(c, "p05")
        assert card["status"] == "stopped", f"收尾事件把中止蓋掉了：{card['status']}"
        assert "異常結束" not in (card.get("report") or ""), card.get("report")
        evs2 = [e["text"] for e in card["timeline"]]
        assert not any("任務中斷" in t for t in evs2), f"中止之後又喊一次中斷：{evs2}"

        # ③ cogito 引擎、cogito 連不上：照樣停得掉，但要說清楚沒叫停它
        main.engine_sent.pop("p05", None)
        class _Down(_FakeHTTP):
            async def post(self, url, **kw):
                raise main.httpx.HTTPError("cogito down")

        main.COGITO_HTTP = "http://fake"
        old_cli = main.httpx.AsyncClient
        main.httpx.AsyncClient = lambda **kw: _Down()
        try:
            main.busy.discard("p12")
            post(c, agent="p12", kind="start", label="cogito 掛了的任務")
            r = stop(c, "p12")
            assert r["ok"], f"cogito 連不上也必須停得掉：{r}"
            assert "p12" not in main.busy
            evs3 = [e["text"] for e in card_of(c, "p12")["timeline"]]
            assert any("沒叫停它" in t for t in evs3), f"要誠實說沒叫停上游：{evs3}"
        finally:
            main.httpx.AsyncClient = old_cli
            main.COGITO_HTTP = ""

        # ④ 沒有 COGITO_HTTP 也能停（先前這條直接回錯，卡片就此永遠開著）
        main.busy.discard("p07")
        post(c, agent="p07", kind="start", label="沒設入口的任務")
        r = stop(c, "p07")
        assert r["ok"] and "p07" not in main.busy, r
        assert card_of(c, "p07")["status"] == "stopped"

        # ⑤ 【卡在但人不 busy】——上個行程留下的殘卡，先前唯一無解的情況：
        # 畫面顯示進行中，中止卻說「沒有進行中的任務」。
        post(c, agent="p08", kind="start", label="上個行程留下的")
        main.busy.discard("p08")            # 模擬：卡還開著，busy 沒了
        r = stop(c, "p08")
        assert r["ok"], f"殘卡也要收得掉：{r}"
        assert card_of(c, "p08")["status"] == "stopped"

        # ⑥ 真的沒事做的人按中止：這時才該說「沒有進行中的任務」
        main.busy.discard("p01")
        post(c, agent="p01", kind="start", label="正常做完的任務")
        post(c, agent="p01", kind="done", label="ok")
        r = stop(c, "p01")
        assert not r["ok"] and "沒有進行中的任務" in r["error"], r
        for aid in ("p05", "p12", "p07", "p08", "p01"):
            main.busy.discard(aid)
        main.engine_sent.clear()
        main.stopped.clear()
        main.save_state()


def cli_mode() -> None:
    """CLI 模式：Claude Code 的事件流 → 一模一樣的 office 投影（走位/泡泡/卡片全共用）。

    用假 CLI 吐【實測抓到的真實形狀】，不燒訂閱額度也不依賴網路。
    """
    import tempfile
    fake = """#!/usr/bin/env python3
import json, os, sys
# 把收到的 --model 原樣回報成 init 的 model——沒傳就報一個假的預設，
# 這樣測試才分得出「有指定」與「用 CLI 自己的設定」
argv = sys.argv[1:]
picked = argv[argv.index("--model") + 1] if "--model" in argv else "cli-自己的預設"
# 把收到的參數留下來：合約測試要確認 session 旗標【真的送到 CLI】，
# 光驗 cli_session_args() 算得對，接線被拔掉一樣不會紅
open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8").write(" ".join(argv) + chr(10))
out = [
 {"type":"system","subtype":"init","model":picked,"cwd":"x",
  "tools":["Read","Edit","Bash","Task"],
  "skills":[{"name":"repo-wiki","description":"養 repo 文件"}],
  "mcp_servers":[{"name":"playwright","status":"connected"}]},
 {"type":"assistant","message":{"content":[
   {"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"a.py"}}]}},
 {"type":"user","message":{"content":[
   {"type":"tool_result","tool_use_id":"t1","content":"1\tprint('hi')","is_error":False}]}},
 {"type":"assistant","message":{"content":[
   {"type":"tool_use","id":"t2","name":"Bash","input":{"command":"go test ./..."}}]}},
 {"type":"user","message":{"content":[
   {"type":"tool_result","tool_use_id":"t2","content":"exit 1: 編譯失敗","is_error":True}]}},
 {"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1788384000,
   "rateLimitType":"five_hour","unifiedWindows":{"five_hour":{"utilization":0.05},
   "seven_day":{"utilization":0.04}}}},
 {"type":"assistant","message":{"content":[{"type":"text","text":"它印出 hi。"}]}},
 {"type":"result","subtype":"success","is_error":False,"num_turns":2,
  "total_cost_usd":0.2677,"result":"它印出 hi。"},
]
for o in out:
    print(json.dumps(o, ensure_ascii=False), flush=True)
"""
    with tempfile.TemporaryDirectory() as tmp:
        bin_ = Path(tmp) / "fakeclaude"
        bin_.write_text(fake.replace("False", "false").replace("'", "'"), encoding="utf-8")
        # 上面那行只是避免 Python bool 混入 JSON 字面；實際用 json.dumps 產生，安全
        bin_.write_text(fake, encoding="utf-8")
        bin_.chmod(0o755)
        old_cmd, main.CLI_CMD = main.CLI_CMD, str(bin_)
        old_ch, main.CHANNELS_DIR = main.CHANNELS_DIR, Path(tmp) / "channels"
        argv_log = Path(tmp) / "argv.log"
        os.environ["FAKE_ARGV_LOG"] = str(argv_log)
        old_sess, main.CLI_SESSION_DIR = main.CLI_SESSION_DIR, Path(tmp) / "sessions"
        main.CLI_SESSION_DIR.mkdir()
        main.engine_sent.clear()   # 這個是會持久化的：先前跑測試留下的殘值會讓斷言錯亂
        try:
            with TestClient(main.app) as c:
                assert main.cli_available(), "假 CLI 應該被視為可用"
                # 引擎選擇：外殼覆蓋 > 人設 > 預設
                assert main.engine_of("p05") == main.ENGINE_COGITO
                assert main.engine_of("p05", "cli") == main.ENGINE_CLI

                main.busy.discard("p05")
                # 記下「現在最新的卡是哪張」：state 檔裡本來就有舊卡，不比對的話
                # 輪詢會立刻拿到一張早就完成的卡，整個測試對著錯的對象斷言（踩過）。
                before = (main.last_report.get("p05") or {}).get("id")
                r = c.post("/office/dispatch", json={"agent": "p05", "text": "看一下 a.py",
                                                     "engine": "cli"}).json()
                assert r["ok"] and r.get("engine") == "cli", r
                card = None
                for _ in range(100):       # 等背景任務開新卡並跑完
                    time.sleep(0.05)
                    cur = main.last_report.get("p05")
                    if cur and cur.get("id") != before and cur["status"] != "working":
                        card = cur
                        break
                assert card and card["status"] == "ok", card
                # 【session 旗標要真的送出去】。第一次沒有舊對話 → --session-id 建立。
                # 沒有它，每次派工都是全新行程、全新失憶——使用者下「繼續」時 agent
                # 根本不知道要繼續什麼（實際回報）。
                sent = argv_log.read_text(encoding="utf-8").splitlines()[-1]
                sid = main.cli_session_id("p05", main.CHANNELS_DIR / "office_p05")
                assert f"--session-id {sid}" in sent, f"argv 沒帶 session：{sent}"
                evs = [e["text"] for e in card["events"]]
                assert any("▸ Read" in t for t in evs), evs          # 工具 → 事件
                assert any("✓ Read" in t for t in evs), evs          # 成功的結果
                assert any("✗ Bash" in t for t in evs), evs          # 失敗的要標成失敗，不能混為一談
                # 【額度事件是例行回報】status=allowed、用量 5% 時什麼都不該講。
                # 先前把每一筆都翻譯成「觸到上限」——看起來很像真的的謊（實際回報）。
                assert not any("額度" in t for t in evs), f"例行的額度回報不該變成警告：{evs}"
                assert any("它印出 hi" in t for t in evs), evs        # 回話
                # 【誠實】訂閱制不按次計費：total_cost_usd 是「換算成 API 會是多少」，
                # 標成花費就是說謊，所以卡片不該有 cost
                assert "cost" not in card, card.get("cost")
                assert "p05" not in main.busy, "收工要釋放員工"
                # CLI 用自己的設定選模型——我們指定不了，但要【講得出來】它用了什麼。
                # 先前的做法是把模型那排藏掉，看起來像功能不見了（實際回報）。
                # 【CLI 模式的能力面板】cogito 沒開時，能力來自 CLI 自己的 init 事件。
                # 先前一律問 cogito，於是純 CLI 用法下面板只剩「取不到能力清單」——
                # 看起來像這個模式沒有能力，其實它有一大把（實際回報）。
                old_http, main.COGITO_HTTP = main.COGITO_HTTP, ""
                main._caps_cache = None
                try:
                    caps = c.get("/office/caps").json()
                    assert caps["ok"], caps
                    assert [t["name"] for t in caps["tools"]] == ["Read", "Edit", "Bash", "Task"], caps
                    assert caps["skills"][0]["name"] == "repo-wiki", caps
                    assert caps["mcp"][0]["name"] == "playwright", caps
                    # 來源要標出來：cogito 與 CLI 的工具集完全不同，混著看比沒有更誤導
                    assert "Claude Code CLI" in caps.get("source", ""), caps
                    # 【跨重啟要留著】能力只在跑過 CLI 任務時才拿得到。不持久化的話，
                    # 每次開 unity_demo 面板都是空的、要先派一次工才看得到（實際回報）。
                    main.save_state()
                    snap = json.loads(main.STATE_FILE.read_text(encoding="utf-8"))
                    assert snap.get("cli_caps", {}).get("tools"), "能力沒被存下來"
                    main.cli_caps.clear()          # 模擬重啟：記憶體清空
                    main._caps_cache = None
                    assert c.get("/office/caps").json()["ok"] is False, "前置條件：清空後應拿不到"
                    main.load_state()              # 重新載入
                    caps2 = c.get("/office/caps").json()
                    assert caps2["ok"] and len(caps2["tools"]) == 4, caps2
                finally:
                    main.COGITO_HTTP, main._caps_cache = old_http, None

                # 沒指定模型＝不帶 --model，交回 CLI 自己的設定
                assert main.cli_model.get("p05") == "cli-自己的預設", main.cli_model

                # 【CLI 也能指定模型】--model 要真的傳下去（先前完全沒接，是實際回報的缺口）
                main.busy.discard("p05")
                b3 = (main.last_report.get("p05") or {}).get("id")
                r = c.post("/office/dispatch", json={"agent": "p05", "text": "換個模型跑",
                                                     "engine": "cli", "model": "claude-haiku-4-5"}).json()
                assert r["ok"] and r.get("model") == "claude-haiku-4-5", r
                for _ in range(100):
                    time.sleep(0.05)
                    cur = main.last_report.get("p05")
                    if cur and cur.get("id") != b3 and cur["status"] != "working":
                        break
                assert main.cli_model.get("p05") == "claude-haiku-4-5", \
                    f"--model 沒傳到 CLI：{main.cli_model.get('p05')!r}"
                # 選了會記住（與 cogito 那條共用 model_sent，語意一致）
                assert main.model_sent.get("p05") == "claude-haiku-4-5", main.model_sent

                # 【CLI ＋ 工作 repo】：先前 CLI 分流在綁 repo 之前就 return，於是選了 repo
                # 等於沒選——worktree 沒開，CLI 在頻道工作區裡跑，然後合理地認定自己在
                # cogito-agent（實際回報的症狀）。這條把「兩件事要能同時成立」釘住。
                src = Path(tmp) / "repos" / "demo-app"
                src.mkdir(parents=True)
                for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                            ["git", "config", "user.name", "t"]):
                    subprocess.run(cmd, cwd=src, check=True)
                (src / "app.py").write_text("x = 1\n")
                subprocess.run(["git", "add", "-A"], cwd=src, check=True)
                subprocess.run(["git", "commit", "-qm", "init"], cwd=src, check=True)
                old_repos, main.REPOS_DIR = main.REPOS_DIR, str(Path(tmp) / "repos")
                try:
                    main.busy.discard("p05")
                    before2 = (main.last_report.get("p05") or {}).get("id")
                    r = c.post("/office/dispatch", json={"agent": "p05", "text": "看一下這個專案",
                                                         "engine": "cli", "repo": "demo-app"}).json()
                    assert r["ok"] and r.get("repo") is True, r
                    wt = main.CHANNELS_DIR / "office_p05" / "demo-app"
                    assert (wt / "app.py").exists(), "CLI 模式也要真的把 worktree 掛出來"
                    for _ in range(100):
                        time.sleep(0.05)
                        cur = main.last_report.get("p05")
                        if cur and cur.get("id") != before2 and cur["status"] != "working":
                            break
                    # CLI 要跑在【worktree 裡】而不是頻道工作區——那是它判斷「我在哪個專案」
                    # 的依據（會往上找 CLAUDE.md、用 git 找 repo 根）
                    card = main.last_report.get("p05")
                    assert card and card.get("workdir", "").endswith("/demo-app"), \
                        f"CLI 應在 worktree 裡跑，實際 workdir={card.get('workdir')!r}"
                finally:
                    main.REPOS_DIR = old_repos
        finally:
            main.CLI_CMD, main.CHANNELS_DIR = old_cmd, old_ch
            main.CLI_SESSION_DIR = old_sess
            os.environ.pop("FAKE_ARGV_LOG", None)
            main.busy.discard("p05")
            # engine_sent 會【持久化】：不 flush 的話，殘值留在 state 檔裡，
            # 下一個測試的 TestClient 啟動時 load_state 又把它讀回來（踩過：
            # 後面的 repo_binding 因此走了 CLI 分支，repo 根本沒綁）。
            main.engine_sent.clear()
            main.model_sent.pop("p05", None)
            main.cli_caps.clear()      # 假 CLI 的能力別留在真實 state 裡
            main.cli_model.pop("p05", None)
            main.save_state()


def status_emote() -> None:
    """頭邊的狀態徽章：掛得上、收得掉，而且【不會留殘影】。

    為什麼要有它：泡泡是轉瞬的（2.5-7 秒消失），講「剛剛發生什麼」；徽章持續掛著，
    講「他現在卡在什麼狀態」——掃一眼就知道誰動不了。額度那條更是先前【完全沒有】
    身體投影的狀態，工作串印一行字，畫面上跟正常工作一模一樣。

    最重要的斷言是最後一段：狀態沒了徽章一定要下來。假投影裡最糟的一種就是
    「事情早就過了，畫面還說他卡著」。
    """
    with TestClient(main.app) as c, c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "waypoints", "agents": [],
                                 "list": main.waypoint_list or ["chair_1"]}))
        aid = "p07"
        main.emote_now.clear()
        main.rate_state.clear()
        main.watering.discard(aid)
        main.pending_approval.pop(aid, None)
        try:
            # ① 等審批 → 靜態藍問號（【不知道期限】的情況：算不出剩多久就別畫倒數）
            assert main.want_emote(aid) == "", "前置條件：什麼事都沒有就不該掛徽章"
            main.pending_approval[aid] = "rm -rf /tmp/x"
            assert main.want_emote(aid) == "wait", main.want_emote(aid)
            asyncio.run(main.sync_emote(aid))
            m = recv(ws, aid, emote=True)
            assert (m["action"], m["target"]) == ("emote", "wait"), m

            # ② 同狀態不重發（工具事件很密，每筆都送會把指令流洗掉）
            asyncio.run(main.sync_emote(aid))
            asyncio.run(main.sync_emote(aid))

            # ③ 額度被擋比「空轉」更該講，但【等人回答】又比額度優先——
            # 兩件事同時成立時只掛一個，掛最阻塞的那個。
            main.rate_state[aid] = "alert"
            main.watering.add(aid)
            assert main.want_emote(aid) == "wait", "等審批要壓過額度與空轉"
            main.pending_approval.pop(aid, None)
            assert main.want_emote(aid) == "alert", "額度被擋要壓過空轉"
            main.rate_state.pop(aid, None)
            assert main.want_emote(aid) == "think", "只剩空轉"

            # ④ 【狀態沒了就要收掉】——這條是整組測試的重點
            main.watering.discard(aid)
            assert main.want_emote(aid) == ""
            asyncio.run(main.sync_emote(aid))
            m = recv(ws, aid, emote=True)
            assert (m["action"], m["target"]) == ("emote", ""), \
                f"狀態過了徽章沒收——這是最糟的一種假投影：{m}"

            # ④-b 【知道期限就畫倒數】：餅圖填滿＋轉紅，同時講進度與急迫。
            # 格數在橋算（跟 office_report 的剩餘秒數同一個基準）——兩邊各算一次，
            # 就會出現「卡片說剩 30 秒、頭上的餅才半滿」這種誰也不能信的畫面。
            main.pending_approval[aid] = "rm -rf /tmp/x"   # ③ 把它 pop 掉了，重新放回來
            main.approval_at[aid] = time.time()
            main.approval_meta[aid] = {"timeout_s": 800}
            assert main.approval_step(aid) == 0, "剛送來要在第 0 格"
            assert main.want_emote(aid) == "timer_0", main.want_emote(aid)
            main.approval_at[aid] = time.time() - 400        # 走了一半
            assert main.approval_step(aid) == 4, main.approval_step(aid)
            main.approval_at[aid] = time.time() - 799        # 快逾時
            assert main.approval_step(aid) == 7, main.approval_step(aid)
            main.approval_at[aid] = time.time() - 9999       # 早就過期也不該爆出範圍
            assert main.approval_step(aid) == 7, "超時要夾在最後一格，不能索引到不存在的圖"
            main.approval_at.pop(aid, None)
            main.approval_meta.pop(aid, None)
            assert main.want_emote(aid) == "wait", "沒有期限資訊就退回靜態問號"
            main.pending_approval.pop(aid, None)
            # 這一段是純算式（want_emote/approval_step），不送指令——徽章早在 ④ 就收了，
            # 去重會擋掉重送，在這裡等指令會直接卡死。

            # ⑤ 看板沒有身體，掛不上去（不能對著空氣送指令）
            asyncio.run(main.emote(main.KANBAN, "wait"))
            assert main.KANBAN not in main.emote_now, "看板不該有徽章"

            # ⑥ 新畫面上線要能重掛：去重的記憶得清掉，否則重整分頁後徽章全不見
            main.pending_approval[aid] = "x"
            asyncio.run(main.sync_emote(aid))
            recv(ws, aid, emote=True)
            ws.send_text(json.dumps({"type": "waypoints", "agents": [],
                                     "list": main.waypoint_list or ["chair_1"]}))
            for _ in range(50):
                time.sleep(0.02)
                if not main.emote_now:
                    break
            assert not main.emote_now, "握手後沒清掉去重記憶，重整分頁徽章就回不來"
        finally:
            main.pending_approval.pop(aid, None)
            main.approval_at.pop(aid, None)
            main.approval_meta.pop(aid, None)
            main.rate_state.clear()
            main.watering.discard(aid)
            main.emote_now.clear()


def proposed_memory_badge() -> None:
    """待審的記憶提案 → 頭上掛寶石。

    為什麼需要：cogito 的 consolidate 會把「這次學到什麼」寫成提案，但【刻意不自動套用】，
    等人 review 才進長期記憶。安全，代價是沒人看就永遠堆著——接這條的當下實測四位員工
    身上已經有 55 條沒人知道的提案。這是最典型的徽章形狀：持續存在、等你處理。

    計數刻意跟 cogito 的 parseProposedMemory 對齊（"- " 開頭且有內容才算）——
    數字跟他們的 review 畫面對不上，比沒有數字更糟。
    """
    import tempfile
    aid = "p12"
    with tempfile.TemporaryDirectory() as tmp:
        ch = Path(tmp) / "channels"
        (ch / f"office_{aid}" / ".claw").mkdir(parents=True)
        f = ch / f"office_{aid}" / ".claw" / main.PROPOSED_FILE.split("/")[-1]
        old_ch, main.CHANNELS_DIR = main.CHANNELS_DIR, ch
        main.memo_pending.clear()
        main.pending_approval.pop(aid, None)
        try:
            # 沒有檔案＝還沒產生過提案，不是錯誤
            assert main.count_proposed(aid) == 0, "沒檔案就該回 0，不該炸"

            f.write_text(
                "<!-- ⚠️ 自動生成的『提案記憶』。\n- 這行在註解裡，不算 -->\n"
                "\n## [user] 來自任務「寫個 md」（2026-09-04T10:00:00+08:00）\n"
                "- 傾向要求輸出成 md 文件\n"
                "- 關心系統性的流程而非單點修補\n"
                "-   \n"                                    # 空 bullet 不算
                "## [style] 來自任務「另一件事」\n"
                "- 說話喜歡先給結論\n", encoding="utf-8")
            n = main.count_proposed(aid)
            assert n == 3, f"該數 3 條（註解內、空 bullet、## 標題都不算），實際 {n}"

            # 刷新 → 掛徽章
            main.refresh_proposed(aid)
            assert main.want_emote(aid) == "idea", main.want_emote(aid)

            # 【優先序】有人在等你決定，比「他學到東西」急得多
            main.pending_approval[aid] = "rm -rf /"
            assert main.want_emote(aid) == "wait", "等審批要壓過待審提案"
            main.pending_approval.pop(aid, None)

            # review 完（cogito 那邊 apply 會清掉條目）→ 徽章要跟著收
            f.write_text("<!-- 只剩表頭 -->\n", encoding="utf-8")
            main.refresh_proposed(aid)
            assert main.want_emote(aid) == "", "提案清掉了徽章沒收——事情過了畫面還說有事"

            # 沒設 COGITO_CHANNELS：整條功能靜默關閉，不是報錯
            f.write_text("- 又有一條了\n", encoding="utf-8")
            main.CHANNELS_DIR = None
            assert main.count_proposed(aid) == 0, "沒設頻道目錄就該安靜關掉"
        finally:
            main.CHANNELS_DIR = old_ch
            main.memo_pending.clear()
            main.pending_approval.pop(aid, None)


def proposed_review() -> None:
    """在外殼審記憶提案：列得出來、編號跟 cogito 一致、放行轉給 cogito 執行。

    【編號一致是這條的重點】。放行是把 `apply memory <編號>` 轉過去給 cogito 跑的，
    橋這邊算錯一個位移，放行的就是別條——而 UPDATE/DELETE 那種會改掉或刪掉既有記憶，
    錯放比不放糟得多。所以文法要跟他們的 parseProposedMemory 對齊：
    `## ` 是任務標題不算、有內容的 `- ` 才算、編號跨標題連號、縮排是附帶欄位。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ch = Path(tmp) / "channels"
        (ch / "office_p07" / ".claw").mkdir(parents=True)
        (ch / "office_p07" / ".claw" / "AGENTS.proposed.md").write_text(
            "<!-- 這段註解裡的 - 假 bullet 不能算 -->\n"
            "## 任務 A\n"
            "- 第一條學到的事\n"
            "  觸發：關鍵字\n"
            "-\n"                                   # 空 bullet：cogito 也不算
            "## 任務 B\n"
            "- UPDATE old-slug 改成新的說法\n"
            "  舊：本來的說法\n"
            "- DELETE stale-slug\n", encoding="utf-8")
        old_ch, main.CHANNELS_DIR = main.CHANNELS_DIR, ch
        old_http, main.COGITO_HTTP = main.COGITO_HTTP, ""
        try:
            items = main.parse_proposed("p07")
            assert [it["n"] for it in items] == [1, 2, 3], f"編號要連號跨標題：{items}"
            assert items[0]["task"] == "任務 A" and items[2]["task"] == "任務 B", items
            assert items[0]["meta"] == ["觸發：關鍵字"], items[0]
            assert items[1]["op"] == "update" and items[2]["op"] == "delete", \
                "會動到既有記憶的要標出來——那不是多記一件事"
            assert items[0]["op"] == "", items[0]
            assert main.count_proposed("p07") == 3, "數量與清單必須同源"

            with TestClient(main.app) as c:
                r = c.get("/office/proposed/p07").json()
                assert r["ok"] and len(r["items"]) == 3, r
                assert r["can_apply"] is False, "沒有 cogito 就放行不了，要先講"
                # 沒有 cogito → 一條都不能動，而且要明說（不是靜默失敗）
                a = c.post("/office/proposed/p07", json={"verb": "apply", "nums": [1]}).json()
                assert a["ok"] is False and "只有它能放行" in a["error"], a
                # 編號越界要擋：轉過去就會放行到別條，或整批被 cogito 拒絕
                main.COGITO_HTTP = "http://fake"
                b = c.post("/office/proposed/p07", json={"verb": "apply", "nums": [9]}).json()
                assert b["ok"] is False and "超出範圍" in b["error"], b
                # verb 白名單：這條會被原樣拼進送給 cogito 的指令字串
                v = c.post("/office/proposed/p07", json={"verb": "drop", "nums": []}).json()
                assert v["ok"] is False, v

                # 轉發成功：指令字串要正確（編號空＝全部）
                sent = []

                class _Rec(_FakeHTTP):
                    async def post(self, url, **kw):
                        sent.append(kw.get("json", {}).get("text"))
                        return _FakeResp()

                old_cl, main.httpx.AsyncClient = main.httpx.AsyncClient, lambda **kw: _Rec()
                try:
                    assert c.post("/office/proposed/p07",
                                  json={"verb": "apply", "nums": [1, 3]}).json()["ok"]
                    assert c.post("/office/proposed/p07",
                                  json={"verb": "reject", "nums": []}).json()["ok"]
                    assert sent == ["apply memory 1 3", "reject memory"], sent
                finally:
                    main.httpx.AsyncClient = old_cl
        finally:
            main.CHANNELS_DIR, main.COGITO_HTTP = old_ch, old_http
            main.memo_pending.pop("p07", None)


def approver_key_separation() -> None:
    """派工權／審批權分離在橋這端的半邊：只有 approve/reject 帶 X-Approver-Token，派工與中止不帶。
    兩把鑰匙各開一扇門——橋若把審批鑰匙也塞進派工請求，等於把兩把鑰匙綁在一起送出去。"""
    old_tok, main.COGITO_HTTP_APPROVER_TOKEN = main.COGITO_HTTP_APPROVER_TOKEN, "approve-key"
    try:
        assert "X-Approver-Token" not in main.cogito_headers("看一下 repo"), "派工不該帶審批鑰匙"
        assert main.cogito_headers("approve")["X-Approver-Token"] == "approve-key"
        assert main.cogito_headers("reject T1")["X-Approver-Token"] == "approve-key"
        assert "X-Approver-Token" not in main.cogito_headers("/stop"), "中止不是審批"
        main.COGITO_HTTP_APPROVER_TOKEN = ""
        assert "X-Approver-Token" not in main.cogito_headers("approve"), "沒設鑰匙就不帶（不送空字串）"
    finally:
        main.COGITO_HTTP_APPROVER_TOKEN = old_tok

def cli_keeps_session() -> None:
    """CLI 派工要接回上一次的對話——否則每次都是全新的行程、全新的失憶。

    實際回報的症狀：中止之後下「繼續」，agent 完全不知道要繼續什麼，只好自己鑽研
    那兩個字。根因不是中止，是【每一次】派工都沒有連續性；中止只是讓它現形。
    cogito 那條本來就是一個頻道一條 session（磁碟上實測累積 30-44 則），
    同一個介面下兩個引擎行為不同、使用者又看不出來，比單純沒有記憶更糟。

    分兩種旗標是【實測】出來的，不是猜的：--session-id 只負責建立，對已存在的 id
    再用一次會直接死（Error: Session ID ... is already in use.，退出碼 1）。
    """
    import tempfile
    ch = Path("/ch/office_p05")
    assert main.cli_session_id("p05", ch) == main.cli_session_id("p05", ch), "同人同目錄要穩定"
    assert main.cli_session_id("p05", ch) != main.cli_session_id("p07", ch), "不同人不能撞"
    # 換工作 repo＝換 cwd＝另一條。Claude Code 的 session 按專案目錄收納，
    # 硬要跨目錄共用只會在 resume 時找不到（實地確認過目錄長相）。
    assert main.cli_session_id("p05", ch) != main.cli_session_id("p05", ch / "repo")

    with tempfile.TemporaryDirectory() as tmp:
        old_dir, main.CLI_SESSION_DIR = main.CLI_SESSION_DIR, Path(tmp)
        try:
            sid = main.cli_session_id("p05", ch)
            assert main.cli_session_args("p05", ch) == ["--session-id", sid], "沒有舊對話要用建立"
            # 有 session 檔了 → 必須改用 --resume，再送 --session-id 會被 CLI 拒絕
            proj = Path(tmp) / "-ch-office-p05"
            proj.mkdir()
            (proj / f"{sid}.jsonl").write_text("{}", encoding="utf-8")
            assert main.cli_session_args("p05", ch) == ["--resume", sid], \
                "有舊對話卻還用 --session-id——CLI 會回 already in use 直接掛掉"
            # 檔案被清掉（使用者手動刪、或工具自己輪替）要能自己退回開新的，不能卡死
            (proj / f"{sid}.jsonl").unlink()
            assert main.cli_session_args("p05", ch) == ["--session-id", sid], \
                "session 檔沒了還硬要 resume，那個員工就再也派不了工"
        finally:
            main.CLI_SESSION_DIR = old_dir


def reject_always_works() -> None:
    """駁回一定收得掉卡；核准送不出去時【不收】卡，而且要講出後果。

    這兩件事刻意【不對稱】，理由是安全而不是一致性：
      - 駁回：逾時的預設行為本來就是自動拒絕，送不到結果也一樣，所以照收卡。
        這保證審批永遠有出路——先前 cogito 沒在跑時按駁回毫無反應（實際踩到）。
      - 核准：審批擋的是高危操作。送不出去卻把卡收掉，使用者會以為 rm -rf 已經
        授權執行了，實際上 agent 會逾時【自動拒絕】——那是相反的結果。
    """
    class _Down(_FakeHTTP):
        async def post(self, url, **kw):
            raise main.httpx.HTTPError("cogito 沒在跑")

    old_client, main.httpx.AsyncClient = main.httpx.AsyncClient, lambda **kw: _Down()
    old_http, main.COGITO_HTTP = main.COGITO_HTTP, "http://fake"
    aid = "p08"
    try:
        with TestClient(main.app) as c:
            # 【核准送不出去 → 卡留著】
            main.pending_approval[aid] = "rm -rf /tmp/x"
            main.approval_at[aid] = time.time()
            r = c.post("/office/dispatch", json={"agent": aid, "text": "approve"}).json()
            assert r["ok"] is False, r
            assert "沒有送到" in r["error"] and "逾時" in r["error"], r["error"]
            assert aid in main.pending_approval, \
                "核准送不出去卻收了卡——使用者會以為高危操作已經授權執行"

            # 【駁回一定收得掉】——即使 cogito 完全連不上
            r = c.post("/office/dispatch", json={"agent": aid, "text": "reject"}).json()
            assert r["ok"] is True, r
            assert r.get("delivered") is False, "送不到就不該說送到了"
            assert aid not in main.pending_approval, "駁回收不掉卡——審批就沒有出路了"
            assert aid not in main.approval_at, "倒數也要一起收（漏一份就是下一個殘影）"
    finally:
        main.httpx.AsyncClient = old_client
        main.COGITO_HTTP = old_http
        main.pending_approval.pop(aid, None)
        main.approval_at.pop(aid, None)
        main.approval_meta.pop(aid, None)
        main.emote_now.clear()
        main.busy.discard(aid)


def roster_carries_badges() -> None:
    """名冊列要帶得出徽章狀態——不然沒有 3D 畫面時它們全部不存在。

    兩個具體的洞（都是實測出來的）：
      ① 看板沒有身體，emote() 對它直接 return。它身上那 33 條待審提案（佔全部六成）
         在辦公室畫面上一條都掛不出來，名冊是唯一露得出來的地方。
      ② 沒建 WebGL 時 3D 是空的，但 README 說好「名冊和工作串照常運作」。

    來源必須是同一個 want_emote()：名冊自己判斷一次的話，兩邊遲早各說各話。
    """
    with TestClient(main.app) as c, c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "waypoints", "agents": [],
                                 "list": main.waypoint_list or ["chair_1"]}))
        aid, kb = "p12", main.KANBAN
        keep = dict(main.memo_pending)
        main.pending_approval.pop(aid, None)
        main.rate_state.clear()
        main.watering.discard(aid)
        try:
            # 【啟動就要是對的】：只靠 30 秒一輪的 sweep，剛開的名冊會說「0 條」——
            # 那不是還沒載入，是一句錯的話。TestClient 進來時 startup 已經跑過了。
            assert main.memo_pending, "啟動時沒刷提案數，名冊頭 30 秒會說謊"

            main.memo_pending[aid] = 0
            d = c.get("/agents").json()
            assert "badge" in d[aid] and "memo" in d[aid], f"名冊沒帶徽章欄位：{d[aid].keys()}"
            assert d[aid]["badge"] == "", d[aid]["badge"]

            # 【看板那 33 條】：沒有身體，但名冊要看得到
            main.memo_pending[kb] = 33
            d = c.get("/agents").json()
            assert kb in d, "看板不在名冊裡，那這條就白做了"
            assert d[kb]["npc"] is False, "前置條件：看板本來就沒有身體"
            assert d[kb]["memo"] == 33, d[kb]
            # 它掛不出 3D 徽章——這正是名冊要補的洞
            asyncio.run(main.emote(kb, "idea"))
            assert kb not in main.emote_now, "看板不該有 3D 徽章（沒有頭）"

            # 【兩條軸並排】：他在等審批，同時還有提案沒人收——擠成一格就得丟掉一個
            main.memo_pending[aid] = 6
            main.pending_approval[aid] = "x"
            d = c.get("/agents").json()
            assert d[aid]["approval"] is True and d[aid]["memo"] == 6, d[aid]

            # 【同一份真相】：badge 就是 want_emote 的輸出，名冊不另外判斷一次
            main.pending_approval.pop(aid, None)
            main.rate_state[aid] = "alert"
            d = c.get("/agents").json()
            assert d[aid]["badge"] == main.want_emote(aid) == "alert", d[aid]["badge"]
        finally:
            main.memo_pending.clear()
            main.memo_pending.update(keep)
            main.pending_approval.pop(aid, None)
            main.rate_state.clear()
            main.emote_now.clear()


def approval_countdown_walks() -> None:
    """倒數會【自己往前走】：格子邊界到了就換一格。

    這條驗的是排程（tick_approval），不是算式（status_emote 已經驗過 approval_step）。
    沒有它，徽章只會在開審批那一刻掛上去、然後整整五分鐘停在第 0 格——
    看起來像個靜態圖示，倒數的意義整個沒了。全域 sweep 是 30 秒一輪，也比格寬還粗。

    期限壓成 1.6 秒（8 格 × 0.2 秒），不然跑一輪要五分鐘。
    """
    sent: list[dict] = []

    async def fake_send(cmd):
        sent.append(cmd)
        return True

    async def drive():
        aid = "p01"
        main.pending_approval[aid] = "x"
        main.approval_at[aid] = time.time()
        main.approval_meta[aid] = {"timeout_s": 1.6}
        main.emote_now.pop(aid, None)
        t = asyncio.create_task(main.tick_approval(aid))
        await asyncio.sleep(1.0)
        # 收卡：迴圈條件不成立，任務自己結束（不需要另一個清理者——這也是這裡要驗的）
        main.approval_at.pop(aid, None)
        main.pending_approval.pop(aid, None)
        await asyncio.wait_for(t, timeout=3)

    old_send = main.send_cmd
    main.send_cmd = fake_send
    try:
        asyncio.run(drive())
    finally:
        main.send_cmd = old_send
        main.pending_approval.pop("p01", None)
        main.approval_at.pop("p01", None)
        main.approval_meta.pop("p01", None)
        main.emote_now.pop("p01", None)

    steps = [c["target"] for c in sent if c.get("action") == "emote"]
    assert len(steps) >= 3, f"倒數沒有自己往前走，只送了 {steps}"
    nums = [int(t.split("_")[1]) for t in steps if t.startswith("timer_")]
    assert len(nums) == len(steps), f"送出的不全是倒數格：{steps}"
    assert nums[0] == 0, f"要從第 0 格起：{nums}"
    assert nums == sorted(nums), f"格數只能往前走，不能倒退：{nums}"
    assert len(set(nums)) == len(nums), f"同一格不該重送（去重壞了）：{nums}"


def caps_refresh() -> None:
    """能力清單會過期重抓：cogito 加掛 MCP／換技能之後，不該非得重啟整個橋才看得到。
    抓失敗時沿用上一份好的——稍舊的清單遠比空白有用。"""
    calls = []

    class _Caps(_FakeHTTP):
        async def get(self, url, **kw):
            calls.append(url)
            n = len(calls)
            if n == 3:                     # 第三次故意失敗，驗「沿用舊的」
                raise main.httpx.HTTPError("fake down")
            return _FakeResp({"tools": [{"name": f"tool{n}", "description": ""}],
                              "skills": [], "mcp": []})

    main.COGITO_HTTP = "http://fake"
    old_client = main.httpx.AsyncClient
    main.httpx.AsyncClient = lambda **kw: _Caps()
    main._caps_cache, main._caps_at = None, 0.0
    try:
        with TestClient(main.app) as c:
            first = c.get("/office/caps").json()
            assert first["tools"][0]["name"] == "tool1", first
            assert "cogito" in first.get("source", ""), "來源要標出來（與 CLI 那份分得開）"
            # TTL 內不重抓
            assert c.get("/office/caps").json()["tools"][0]["name"] == "tool1"
            assert len(calls) == 1, f"TTL 內不該重問，問了 {len(calls)} 次"
            # 過期就重抓
            main._caps_at -= main.CAPS_TTL + 1
            assert c.get("/office/caps").json()["tools"][0]["name"] == "tool2", "過期要重抓"
            # 重抓失敗：沿用上一份好的，不要變空白
            main._caps_at -= main.CAPS_TTL + 1
            again = c.get("/office/caps").json()
            assert again["ok"] and again["tools"][0]["name"] == "tool2", f"失敗時要沿用舊的：{again}"
    finally:
        main.httpx.AsyncClient = old_client
        main.COGITO_HTTP = ""
        main._caps_cache, main._caps_at = None, 0.0


def rate_limit_wording() -> None:
    """額度事件三態：例行安靜、快滿了提醒、真的被擋才說被擋。

    投影誠實不只是「不要假裝成功」，也包括【不要假裝有事發生】。
    """
    base = {"status": "allowed", "resetsAt": 1788384000, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 0.05},
                               "seven_day": {"utilization": 0.04}}}
    assert main.rate_limit_line(base) == "", "例行回報（用量 5%）不該講話"
    near = dict(base, unifiedWindows={"five_hour": {"utilization": 0.93}})
    assert "93%" in main.rate_limit_line(near), "快用完了要提醒，數字要是真的"
    hit = dict(base, status="rejected")
    line = main.rate_limit_line(hit)
    assert "已達上限" in line and "重置" in line, line
    assert main.rate_limit_line({}) == "" and main.rate_limit_line(None) == ""  # 壞資料不亂講


def model_per_agent() -> None:
    """模型是【員工的屬性】：persona 有 model 就隨派工送給 cogito；沒有就不送
    （不能無聲覆蓋使用者用 `model` 指令選的）。收工揭露的是【實際跑的】那個。"""
    sent = []

    class _Rec(_FakeHTTP):
        async def post(self, url, **kw):
            sent.append(kw.get("json", {}))
            return _FakeResp()

    main.COGITO_HTTP = "http://fake"
    old_client = main.httpx.AsyncClient
    main.httpx.AsyncClient = lambda **kw: _Rec()
    try:
        with TestClient(main.app) as c:
            # 名冊要揭露設定值（外殼才畫得出「這位員工跑什麼」）
            roster = c.get("/agents").json()
            assert roster["p19"]["model"] == "claude-opus-5", roster["p19"]
            assert roster["p01"]["model"] == "", roster["p01"]

            main.busy.discard("p19")
            c.post("/office/dispatch", json={"agent": "p19", "text": "做架構決策"})
            assert sent[-1].get("model") == "claude-opus-5", sent[-1]

            # 沒設 model 的員工：payload 裡【不該有】這個鍵——帶空字串會把對面設定清掉
            main.busy.discard("p01")
            c.post("/office/dispatch", json={"agent": "p01", "text": "寫個需求"})
            assert "model" not in sent[-1], sent[-1]

            # 外殼的臨時覆蓋：優先於人設
            main.busy.discard("p19")
            c.post("/office/dispatch", json={"agent": "p19", "text": "這次用便宜的",
                                              "model": "claude-haiku-4-5"})
            assert sent[-1]["model"] == "claude-haiku-4-5", sent[-1]
            # 覆蓋【不是隱形狀態】：cogito 那邊是 session 級持久的，所以橋要記著並揭露，
            # 否則選一次 opus 就永遠是 opus 而畫面上看不出來。
            eff = c.get("/office/models").json()["effective"]
            assert eff["p19"] == "claude-haiku-4-5", eff
            # 沒選就沿用人設，但【目前實際會用的】仍是上次那個覆蓋（誠實反映 cogito 的狀態）
            main.busy.discard("p19")
            c.post("/office/dispatch", json={"agent": "p19", "text": "沒選模型"})
            assert sent[-1]["model"] == "claude-opus-5", sent[-1]
            # 還原：把覆蓋收回啟動預設（與聊天端 `model reset` 同一個字）
            main.busy.discard("p19")
            c.post("/office/dispatch", json={"agent": "p19", "text": "還原",
                                              "model": main.MODEL_RESET})
            assert sent[-1]["model"] == main.MODEL_RESET, sent[-1]
            assert c.get("/office/models").json()["effective"]["p19"] == "claude-opus-5", "還原後回到人設"

            # 清單優先問 cogito（→ 官方 /v1/models）。這裡的假 cogito 沒有 /models，
            # 所以走【降級】：用後備清單（人設裡指派過的），而且 source 要講出來——
            # 降級不能是無聲的，否則使用者以為自己在看官方清單。
            m = c.get("/office/models").json()
            assert m["source"] == "local", m["source"]
            assert [x["id"] for x in m["models"]] == ["claude-haiku-4-5", "claude-opus-5"], m["models"]

            # 【橋自己問官方】cogito 沒開時不該掉到只剩人設那兩個——CLI 模式根本不經過
            # cogito，清單卻綁著它開不開，那是實際回報的問題。
            class _M:
                def __init__(self, i, n): self.id, self.display_name = i, n

            class _Page:
                data = [_M("claude-opus-5", "Claude Opus 5"),
                        _M("claude-sonnet-5", "Claude Sonnet 5"),
                        _M("claude-haiku-4-5-20251001", "Claude Haiku 4.5")]

            class _Models:
                async def list(self, **kw):
                    return _Page()

            class _Client:
                models = _Models()

            old_client2, main.client = main.client, _Client()
            main._api_models = ([], 0.0)
            try:
                m = c.get("/office/models").json()
                assert m["source"] == "api", m["source"]
                assert len(m["models"]) == 3 and m["models"][0]["name"] == "Claude Opus 5", m
            finally:
                main.client = old_client2
                main._api_models = ([], 0.0)

            # 成功路徑：cogito 答得出來時【用它的】，不用後備清單。
            # 官方清單會有本地沒有的型號（那正是重點——手動表必然落後於發布）。
            class _WithModels(_Rec):
                async def get(self, url, **kw):
                    assert url.endswith("/models"), url
                    return _FakeResp({"models": [{"id": "claude-fable-5-1", "name": "Claude Fable 5.1"},
                                                 {"id": "claude-opus-5", "name": "Claude Opus 5"}],
                                      "source": "live"})

            main.httpx.AsyncClient = lambda **kw: _WithModels()
            m = c.get("/office/models").json()
            assert m["source"] == "live", m
            assert [x["id"] for x in m["models"]] == ["claude-fable-5-1", "claude-opus-5"], m
            assert m["models"][0]["name"] == "Claude Fable 5.1", "顯示名要帶過來（比 id 好認）"
            main.httpx.AsyncClient = lambda **kw: _Rec()

            # 揭露：done 帶的是實際跑的模型，進卡片
            post(c, agent="p19", kind="start", label="架構決策")
            post(c, agent="p19", kind="done", label="ok", cost=0.5, model="claude-opus-5")
            r = c.get("/office/report/p19").json()
            assert r["model"] == "claude-opus-5", r.get("model")

            # 單價是估的要標出來（模型沒登記定價）；實價不標
            post(c, agent="p19", kind="start", label="用了沒登記的模型")
            post(c, agent="p19", kind="done", label="ok", cost=0.9, model="某個新模型", cost_est=True)
            r = c.get("/office/report/p19").json()
            assert r["cost"] == 0.9 and r["cost_est"] is True, r
            post(c, agent="p19", kind="start", label="用有登記的")
            post(c, agent="p19", kind="done", label="ok", cost=0.9, model="claude-opus-5")
            assert "cost_est" not in c.get("/office/report/p19").json(), "實價不該標成估價"

            # 未知就不給——寧可空白，不要編一個 id 讓人以為知道
            post(c, agent="p19", kind="start", label="沒跑到模型就掛了")
            post(c, agent="p19", kind="done", label="error", detail="爆了")
            assert "model" not in c.get("/office/report/p19").json()
    finally:
        main.httpx.AsyncClient = old_client
        main.COGITO_HTTP = ""
        main.busy.discard("p19")
        main.model_sent.clear()


def steer_dispatch() -> None:
    """插話（steer→constrain→stop 的第一階）：工作中放行 /steer 轉發 cogito、
    投影進工作串；閒著時擋下——代發成新任務會把「糾正」靜默升級成「開工」。"""
    sent = []

    class _Rec(_FakeHTTP):
        async def post(self, url, **kw):
            sent.append(kw.get("json", {}).get("text"))
            return _FakeResp()

    main.COGITO_HTTP = "http://fake"
    old_client = main.httpx.AsyncClient
    main.httpx.AsyncClient = lambda **kw: _Rec()
    try:
        with TestClient(main.app) as c:
            # 閒著：插不了話，且【不能】轉發（送過去就變成一句新訊息）
            main.busy.discard("p07")
            r = c.post("/office/dispatch", json={"agent": "p07", "text": "/steer 先查快取"}).json()
            assert r["ok"] is False and "沒在工作中" in r["error"], r
            assert sent == [], f"閒置時不該轉發 cogito：{sent}"

            # 工作中：放行、原文轉發、工作串留痕
            post(c, agent="p07", kind="start", label="盤點依賴")
            r = c.post("/office/dispatch", json={"agent": "p07", "text": "/steer 別再讀 lock 檔，直接看 go.mod"}).json()
            assert r["ok"], r
            assert sent == ["/steer 別再讀 lock 檔，直接看 go.mod"], sent
            evs = [e["text"] for e in c.get("/office/report/p07").json()["timeline"]]
            assert any("🧑‍💼 老闆插話：別再讀 lock 檔" in t for t in evs), evs

            # 空插話擋下
            r = c.post("/office/dispatch", json={"agent": "p07", "text": "/steer"}).json()
            assert r["ok"] is False and "空的" in r["error"], r
            post(c, agent="p07", kind="done", label="ok")
    finally:
        main.httpx.AsyncClient = old_client
        main.COGITO_HTTP = ""
        main.busy.discard("p07")


def repo_binding() -> None:
    """任務綁真實 repo：白名單比對、worktree 掛進頻道工作區、任務文字帶工作說明、
    二次派工沿用同一個 worktree；控制動詞（/steer 等）不包裝。"""
    import tempfile
    sent = []

    class _Rec(_FakeHTTP):
        async def post(self, url, **kw):
            sent.append(kw.get("json", {}).get("text"))
            return _FakeResp()

    with tempfile.TemporaryDirectory() as tmp:
        # 造一個真的 git repo 當「你的專案」
        src = Path(tmp) / "repos" / "demo-app"
        src.mkdir(parents=True)
        for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=src, check=True)
        (src / "app.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "-A"], cwd=src, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=src, check=True)

        old_repos, main.REPOS_DIR = main.REPOS_DIR, str(Path(tmp) / "repos")
        old_ch, main.CHANNELS_DIR = main.CHANNELS_DIR, Path(tmp) / "channels"
        main.COGITO_HTTP = "http://fake"
        old_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = lambda **kw: _Rec()
        try:
            with TestClient(main.app) as c:
                assert [r["name"] for r in c.get("/office/repos").json()["repos"]] == ["demo-app"]
                # 帶 agent：還沒掛過 → bound=False
                assert c.get("/office/repos", params={"agent": "p05"}).json()["repos"][0]["bound"] is False

                # 排序：名稱 vs 最近動過。造第二個 repo，讓兩種排法答案【相反】——
                # 不相反的話測試等於沒測（兩種排序碰巧同序，拔掉排序也不會紅）。
                older = Path(tmp) / "repos" / "zzz-newer"
                older.mkdir(parents=True)
                subprocess.run(["git", "init", "-q"], cwd=older, check=True)
                os.utime(older / ".git", (time.time() + 500, time.time() + 500))  # 它「最近動過」
                by_name = [r["name"] for r in c.get("/office/repos", params={"sort": "name"}).json()["repos"]]
                by_time = [r["name"] for r in c.get("/office/repos", params={"sort": "recent"}).json()["repos"]]
                assert by_name == ["demo-app", "zzz-newer"], by_name
                assert by_time == ["zzz-newer", "demo-app"], by_time
                assert all(r["mtime"] > 0 for r in c.get("/office/repos").json()["repos"])

                main.busy.discard("p05")
                r = c.post("/office/dispatch", json={"agent": "p05", "text": "修掉啟動 crash",
                                                     "repo": "demo-app"}).json()
                assert r["ok"], r
                wt = Path(tmp) / "channels" / "office_p05" / "demo-app"
                assert (wt / "app.py").exists(), "worktree 沒掛進頻道工作區"
                # 分支立刻出現在原 repo（worktree 共用物件庫——驗收不必 fetch）
                br = subprocess.run(["git", "-C", str(src), "branch", "-a"],
                                    capture_output=True, text=True).stdout
                assert "office/p05-" in br, br
                assert "【工作 repo】./demo-app/" in sent[-1], "任務文字沒帶工作說明"
                assert "不要 push" in sent[-1]

                # 二次派工：沿用，不炸也不多開分支
                r = c.post("/office/dispatch", json={"agent": "p05", "text": "接著加測試",
                                                     "repo": "demo-app"}).json()
                assert r["ok"], r
                n = subprocess.run(["git", "-C", str(src), "branch", "-a"],
                                   capture_output=True, text=True).stdout.count("office/p05-")
                assert n == 1, f"同員工同 repo 應沿用分支，開了 {n} 條"

                # 掛過之後 bound=True，且排在清單最前面——「最近使用」用磁碟上的真實
                # worktree 判斷，不記在瀏覽器（換瀏覽器/清快取都還在，且天生 per-員工）。
                # 另造一個字母序更前面的 repo，證明排序真的是 bound 優先而不是碰巧。
                other = Path(tmp) / "repos" / "aaa-other"
                other.mkdir(parents=True)
                subprocess.run(["git", "init", "-q"], cwd=other, check=True)
                # 「進行中」是相關性、不是排序——兩種排法下都要置頂（連最近動過的 zzz 也壓得住）
                for mode in ("name", "recent"):
                    rs = c.get("/office/repos", params={"agent": "p05", "sort": mode}).json()["repos"]
                    assert rs[0]["name"] == "demo-app", f"{mode} 排序下進行中的沒置頂：{[r['name'] for r in rs]}"
                rs = c.get("/office/repos", params={"agent": "p05", "sort": "name"}).json()["repos"]
                assert [r["name"] for r in rs][:2] == ["demo-app", "aaa-other"], \
                    f"置頂之後其餘要照選的排序：{[r['name'] for r in rs]}"
                assert rs[0]["bound"] is True and rs[1]["bound"] is False
                # 沒帶 agent 就沒有 bound 欄位（那是「對誰而言」的事實，沒指定人就答不出來）
                assert "bound" not in c.get("/office/repos").json()["repos"][0]

                # 白名單：不認識的名字/路徑一律拒收。
                # ⚠ 驗【錯誤訊息】而不只是 ok=False——拔掉白名單，越界路徑最後也會因為
                # 「git worktree 開不出來」回 false，那種綠是假的（封存板 f= 的同一課）。
                # 判準是「有沒有走到動 git 那一步」。
                for bad in ("../../etc", "沒這個"):
                    r = c.post("/office/dispatch", json={"agent": "p05", "text": "x",
                                                         "repo": bad}).json()
                    assert r["ok"] is False and "不認識的 repo" in r["error"], (bad, r)
                # 控制動詞不包裝：工作中 /steer 帶 repo 也只送原文
                main.busy.add("p05")
                c.post("/office/dispatch", json={"agent": "p05", "text": "/steer 別動 schema",
                                                 "repo": "demo-app"})
                assert sent[-1] == "/steer 別動 schema", sent[-1]
        finally:
            main.REPOS_DIR, main.CHANNELS_DIR = old_repos, old_ch
            main.httpx.AsyncClient = old_client
            main.COGITO_HTTP = ""
            main.busy.discard("p05")


def git_lens() -> None:
    """工作區在 worktree 裡改用 git 鏡頭：回答「他改了什麼」而不是「什麼時候改的」。

    mtime 在 worktree 裡本來就不可信——checkout 會把每個檔案的 mtime 蓋成當下，
    綁完 repo 的十分鐘內整個專案都會被標成「剛動過」。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "repos" / "demo-app"
        (src / "lib").mkdir(parents=True)
        for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=src, check=True)
        (src / "app.py").write_text("print(1)\n")
        (src / "keep.py").write_text("# 沒人動我\n")
        (src / "lib" / "deep.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=src, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=src, check=True)

        old_repos, main.REPOS_DIR = main.REPOS_DIR, str(Path(tmp) / "repos")
        old_ch, main.CHANNELS_DIR = main.CHANNELS_DIR, Path(tmp) / "channels"
        try:
            wt_name, _ = main.bind_repo("p12", {"name": "demo-app", "path": str(src)})
            wt = main.CHANNELS_DIR / "office_p12" / wt_name
            # 工作區【根】不套鏡頭：那裡不是我們掛的 worktree，mtime 才是對的權威。
            # ⚠ 這條要在「工作區本身就在一個 git repo 裡」的前提下驗才有意義——那正是
            # 真實部署（CHANNELS_DIR 在 cogito 的 workspace 底下，而 workspace 自己是
            # git repo）。少了這個前提，rev-parse 直接失敗，守門根本沒被走到＝假綠。
            for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                        ["git", "config", "user.name", "t"]):
                subprocess.run(cmd, cwd=main.CHANNELS_DIR.parent, check=True)
            r0 = main.office_ws("p12", "")
            assert "git" not in r0, f"外層 repo 的狀態不該被當成員工的改動：{r0.get('git')}"

            # 員工做了三件事：改一個檔（已 commit）、改一個深層檔（未 commit）、留一個新檔
            (wt / "app.py").write_text("print(1)\nprint(2)\n")
            subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-qm", "改了 app"], cwd=wt, check=True)
            (wt / "lib" / "deep.py").write_text("x = 2\n")
            (wt / "報告.md").write_text("做完了\n")

            r = main.office_ws("p12", wt_name)
            g = r["git"]
            assert g["repo"] == "demo-app" and g["branch"].startswith("office/p12-"), g
            assert g["commits"] == 1, f"應算得出 1 個 commit：{g}"
            assert g["total"] == 3, f"改了 3 個（app.py/deep.py/報告.md）：{g}"
            assert "_root" not in g and "changed" not in g, "內部欄位不該外送"
            ents = {e["name"]: e for e in r["entries"]}
            assert ents["app.py"]["git"] == "M", ents["app.py"]        # 已 commit 的也算
            assert ents["報告.md"]["git"] == "?", ents["報告.md"]        # 未加入的新檔
            assert "git" not in ents["keep.py"], "沒改的檔案要留白——每列都標等於沒標"
            # 資料夾標「底下幾個檔有動」：不然改動藏在深層目錄裡完全看不出來
            assert ents["lib"]["git_n"] == 1, ents["lib"]

            # 進到子目錄一樣有鏡頭（不是只有 worktree 根那一層）
            deep = main.office_ws("p12", f"{wt_name}/lib")
            assert deep["git"]["repo"] == "demo-app"
            assert {e["name"]: e.get("git") for e in deep["entries"]} == {"deep.py": "M"}

            # 沒有出發點（此功能之前建的 worktree）：只講分支，不猜 diff
            subprocess.run(["git", "-C", str(wt), "config", "--unset", main.GIT_BASE_KEY], check=True)
            g2 = main.office_ws("p12", wt_name)["git"]
            assert "note" in g2 and "total" not in g2, f"沒基準就不該給 diff：{g2}"
        finally:
            main.REPOS_DIR, main.CHANNELS_DIR = old_repos, old_ch


def schedule_jobs() -> None:
    """班表：到點派工（走一般 dispatch，投影免費）、同一小時不重複、人在忙跳過並留痕。"""
    import tempfile
    sent = []

    class _Rec(_FakeHTTP):
        async def post(self, url, **kw):
            sent.append(kw.get("json", {}).get("text"))
            return _FakeResp()

    with tempfile.TemporaryDirectory() as tmp:
        sched = Path(tmp) / "schedule.json"
        now = time.localtime()
        sched.write_text(json.dumps([{"name": "巡邏", "weekday": now.tm_wday, "hour": now.tm_hour,
                                      "agent": "p07", "text": "例行巡檢"},
                                     # 沒有 weekday＝每天（老徐的每日趨勢就是這種）；欄位缺不能等於永遠不跑
                                     {"name": "每日趨勢", "hour": now.tm_hour,
                                      "agent": "p19", "text": "整理趨勢"}], ensure_ascii=False))
        old_file, main.SCHEDULE_FILE = main.SCHEDULE_FILE, sched
        main.sched_last.clear()
        main.COGITO_HTTP = "http://fake"
        old_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = lambda **kw: _Rec()
        main.busy.difference_update({"p07", "p19"})
        # 到點：派一次（run_due_jobs 不需要 HTTP 伺服器——它自己呼叫 dispatch 函式）
        asyncio.run(main.run_due_jobs(now))
        assert sent == ["例行巡檢", "整理趨勢"], f"每日任務（沒有 weekday）該在到點時派出：{sent}"
        # 同一小時再查：不重複
        asyncio.run(main.run_due_jobs(now))
        assert sent == ["例行巡檢", "整理趨勢"], f"同一小時重複觸發：{sent}"
        # 下一小時且人在忙：跳過＋工作串留痕
        main.sched_last.clear()
        main.busy.update({"p07", "p19"})
        asyncio.run(main.run_due_jobs(now))
        assert sent == ["例行巡檢", "整理趨勢"], "忙碌時不該派"
        evs = [e["text"] for e in (main.last_report.get("p07") or {"events": []})["events"]]
        assert any("這輪跳過" in t for t in evs), evs
        main.busy.difference_update({"p07", "p19"})
        main.httpx.AsyncClient = old_client
        main.COGITO_HTTP = ""
        main.SCHEDULE_FILE = old_file
        main.sched_last.clear()


def full_stream() -> None:
    """工作串不再是 dashboard 的殘缺版：msg 全文入卡（指示常在尾巴）、result/error 帶內容。

    「dashboard 看得到、辦公室看不到」的漏斗都在橋端的二次剪裁——cogito 已替每種事件
    截好長度，橋不再剪。實際回報：對照 run view 才發現訊息不完整，不知道下一步怎麼操作。
    """
    with TestClient(main.app) as c:
        main.busy.discard("p12")
        post(c, agent="p12", kind="start", label="整理規格")
        # ① msg：600 字、行動指示在最尾巴——先前砍到 200 字，尾巴必丟
        body = "規格整理如下：" + "細節" * 290 + "【下一步請回覆採用方案 B】"
        post(c, agent="p12", kind="msg", label=body)
        evs = [e["text"] for e in c.get("/office/report/p12").json()["timeline"]]
        assert any(t.endswith("【下一步請回覆採用方案 B】") for t in evs), \
            f"msg 的尾巴（行動指示）被砍掉了：{[t[-30:] for t in evs]}"
        # ② result：先前只畫「✓ 工具名」，cogito 帶的結果預覽整個被丟
        post(c, agent="p12", kind="result", label="bash", detail="3 處 TODO，都在 tools/")
        evs = [e["text"] for e in c.get("/office/report/p12").json()["timeline"]]
        assert "✓ bash｜3 處 TODO，都在 tools/" in evs, evs
        # ③ error：detail 不再砍在 120 字
        long_err = "編譯失敗：" + "錯" * 150
        post(c, agent="p12", kind="error", label="go", detail=long_err)
        evs = [e["text"] for e in c.get("/office/report/p12").json()["timeline"]]
        assert any(t == f"✗ go：{long_err}" for t in evs), [t[-20:] for t in evs]
        # ④ chat 路同步放寬（同一個對話窗，兩條路不能一寬一窄）
        chat = "好的老闆，" + "說明" * 200 + "【收尾：明天給你完整報告】"
        c.post("/office/chat", json={"agent": "office:p12", "text": chat})
        evs = [e["text"] for e in c.get("/office/report/p12").json()["timeline"]]
        assert any(t.endswith("【收尾：明天給你完整報告】") for t in evs), "chat 尾巴被砍"
        post(c, agent="p12", kind="done", label="ok")


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
            # owner_id 是「點卡片跳到那個人的工作串」的依據。名字給人看、id 給程式用，
            # 兩個都要回——只回名字的話前端還得自己反查一次名冊，同一張對照表寫兩份。
            assert by["doing"][0]["owner_id"] == "p12", "缺 owner_id，卡片就跳不過去"
            assert by["todo"][0]["owner_id"] is None, "沒有 owner 的卡不該有 owner_id"

            # 守則要求主持人把 owner 寫成【人名】，那才是實際會走的路徑（上面那筆是舊格式的
            # persona id）。兩種都要吃得下，不然規範一改板子就跳不動。
            (wd / "board.json").write_text(json.dumps({
                "task": "x", "tasks": [{"id": "ui", "title": "面板", "deps": [],
                                        "status": "doing", "owner": "小葵"}]},
                ensure_ascii=False), encoding="utf-8")
            card = c.get("/office/board").json()["columns"][2]["cards"][0]
            assert (card["owner"], card["owner_id"]) == ("小葵", "p12"), card

            # live：主持人沒在跑的時候，板子上的「進行中」是舊資料——畫面要講出來，不能
            # 讓人以為現在有人在做。這是投影誠實，不是裝飾。
            main.busy.discard(main.KANBAN)
            assert c.get("/office/board").json()["live"] is False
            main.busy.add(main.KANBAN)
            assert c.get("/office/board").json()["live"] is True
            main.busy.discard(main.KANBAN)

            # 主持人實際上照【主題】取名（board-multitenant.json），不是守則寫的 board.json。
            # 跟模型爭檔名是打不贏的仗，而且一個主題一塊板其實更合理——所以橋這邊讓步。
            # 不吃這種命名的後果很嚴重：整輪都被當成「還在開會」，人永遠不散會、板子也不顯示。
            (wd / "board.json").unlink()
            (wd / "board-multitenant.json").write_text(json.dumps(
                {"task": "多租戶", "tasks": [{"id": "iso", "title": "資料隔離", "deps": [],
                                          "status": "doing", "owner": "阿哲"}]},
                ensure_ascii=False), encoding="utf-8")
            assert c.get("/office/board").json()["task"] == "多租戶", "主題命名的板子沒被認出來"
            assert main.in_meeting(main.KANBAN) is False, "有板子就不該還算在開會"

            # 封存檔（點號分隔）不該被撿回來當「目前這塊板」
            (wd / "board-multitenant.json").rename(wd / "board.0811-130722.json")
            assert main.board_file() is None, "封存的舊板被當成現行板了"
    main.CHANNELS_DIR = None


def camera() -> None:
    """鏡頭跟事件走：每一級都要對得上【真實事件源】，不為了讓相機有事做而降門檻。

    規格出自團隊自己開的那場會（meeting-camera-follow.md）：地圖 29 格寬之後，
    A（縮小看全景）vs B（跟事件走）選 B——細節是投影誠實的載體，縮四成的全景會糊掉細節。

    ⚠ 這支【攔 send_cmd】而不是從 WS 收：收不到訊息時 receive_text 會永遠等下去，
    測試就從紅字變成掛住——那比失敗更糟，CI 上看起來像當機。
    """
    shots, real = [], main.send_cmd

    async def spy(cmd):
        if cmd.get("action") == "focus":
            shots.append(cmd)
        return await real(cmd)

    main.send_cmd = spy
    try:
        with TestClient(main.app) as c:
            main.busy.clear(); main.occupied.clear(); main.pending_approval.clear()

            # ② 需老闆決策：球在他手上，這是最高的非手動級
            c.post("/office/chat", json={"agent": "p17",
                                         "text": main.APPROVAL_PREFIX + "\n要不要刪這個目錄"})
            assert shots and shots[-1]["level"] == main.CAM_DECISION \
                and shots[-1]["agents"] == ["p17"], shots[-1:]

            # ④ 失敗異常
            shots.clear()
            c.post("/office/event", json={"agent": "p17", "kind": "error",
                                          "label": "bash", "detail": "炸了"})
            assert shots and shots[-1]["level"] == main.CAM_FAILURE \
                and shots[-1]["agents"] == ["p17"], shots[-1:]

            # 看板沒有身體，框不到它——送過去 Unity 查不到 agent_id，整包落空
            shots.clear()
            asyncio.run(main.focus([main.KANBAN, "p17"], main.CAM_MEETING))
            assert shots[-1]["agents"] == ["p17"], f"看板被當成可以框住的人：{shots[-1]}"

            # ① 手動聚焦：點誰就鎖誰，這是最高級。點看板（沒有身體）＝解鎖回基態。
            shots.clear()
            assert c.post("/office/focus", json={"agent": "p17"}).json()["locked"] is True
            assert shots[-1] == {"action": "focus", "agents": ["p17"],
                                 "level": main.CAM_MANUAL}, shots[-1]
            shots.clear()
            assert c.post("/office/focus", json={"agent": "kanban"}).json()["locked"] is False
            assert shots[-1]["agents"] == [] and shots[-1]["level"] == main.CAM_MANUAL, \
                f"解鎖也要用手動級送，否則鎖定中的鏡頭不接受：{shots[-1]}"
    finally:
        main.send_cmd = real
        main.pending_approval.clear()
        main.busy.clear()


def standup_meeting() -> None:
    """協作模式的【會議階段】把人叫進會議室入座，上板之後才各自回工位。

    投影的差異只有「去哪裡」，因為真實世界的差異也只有這個。判準必須跟任務板面板一致
    （沒有 board.json＝還在開會）——畫面寫著「會議進行中」、人卻坐回工位，那是兩個投影
    在說不同的話。"""
    import tempfile
    with TestClient(main.app) as c, c.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "waypoints", "agents": [],
                                 "list": main.MEET_SPOTS + ["chair_2"]}))
        with tempfile.TemporaryDirectory() as tmp:
            main.CHANNELS_DIR = Path(tmp)
            wd = Path(tmp) / f"office_{main.KANBAN}"
            wd.mkdir(parents=True)
            main.busy.clear(); main.sub_active.clear(); main.occupied.clear()
            main.history.clear(); main.last_report.clear()

            # 工作區裡躺著【上一輪】的板子時，這一輪照樣要開會。
            # 主持人照主題取名（board-<主題>.json），舊板子會一直留在工作區——只看
            # 「有沒有板子」的話，第二輪起從第一秒就被判成「會議已結束」，沒有人會進會議室。
            # 實測踩過：board-visitor.json 與 board-westwing-spaces.json 躺在那裡，
            # 整輪都沒開成會，六個人直接各自做事。所以比的是【時間】不是【有無】。
            old = wd / "board-上一輪.json"
            old.write_text('{"task":"舊的","tasks":[]}', encoding="utf-8")
            os.utime(old, (time.time() - 3600, time.time() - 3600))

            # 還沒上板＝會議階段：被派的人進會議室入座
            post(c, agent=main.KANBAN, kind="start", label="開會")
            assert main.in_meeting(main.KANBAN), "上一輪的舊板子讓這一輪直接跳過會議"
            post(c, agent=main.KANBAN, kind="tool", label="spawn_subagent")
            who = [n for lst in main.sub_active.values() for n in lst][0]
            assert main.occupied[who] in main.MEET_SPOTS, \
                f"會議階段該進會議室，實際去了 {main.occupied[who]}"

            # 第二個人要坐【不同】的位置，不能疊在一起
            post(c, agent=main.KANBAN, kind="tool", label="spawn_subagent")
            spots = [main.occupied[n] for lst in main.sub_active.values() for n in lst]
            assert len(set(spots)) == len(spots), f"兩個人坐同一格：{spots}"

            # background=true 的 spawn 會【立刻】回一句啟動回執，那不是成果。
            # 照收的話卡片當場變「已完成」、報告寫著啟動訊息、人也被放出去自由走動——
            # 實測回報：三張卡在同一秒全變已完成，內容都是「已在背景啟動子 agent…」。
            post(c, agent=main.KANBAN, kind="result", label="spawn_subagent",
                 detail="🌀 已在背景啟動子 agent [老徐]（ID: bg-1）。要等它交件就用 subagent_await")
            open_now = [n for lst in main.sub_active.values() for n in lst]
            assert len(open_now) == 2, f"啟動回執把委派卡關掉了：剩 {open_now}"

            # 真正的收件在 subagent_await 的結果裡，逐行對人設名收。
            # 一個 ✅ 一個 🟢＝只該收掉一張：🟢 是「這次沒等到」，卡片要繼續開著。
            post(c, agent=main.KANBAN, kind="result", label="subagent_await",
                 detail="背景子 agent bg-1 []：✅ 已完成\n我的意見\n"
                        "背景子 agent bg-2 []：🟢 執行中，尚無結果。")
            left = [n for lst in main.sub_active.values() for n in lst]
            assert len(left) == 1, f"await 該只收掉 ✅ 那一張，實際剩 {left}"

            # 每張卡只放【自己那一段】。整包塞進去的話三個人的卡片會一模一樣，而且開頭是
            # 「背景子 agent bg-1 […]：✅ 已完成」這種收件標頭——那是給主持人看的格式，
            # 不是那個人的意見。實測踩過：老徐的卡片上是三人份的原始輸出。
            closed = next(n for n in open_now if n not in left)
            rep = main.last_report[closed]["report"]
            assert rep == "我的意見", f"卡片該只放自己那段，實際是 {rep!r}"

            # 而且要【一邊一個】：長桌兩側各三個位子，照 a1,a2,a3 順序填的話兩個人會擠在
            # 同一側、對面空著，看起來像在罰站而不是在談事情。
            sides = {s[len("meet_"):][0] for s in spots}
            assert sides == {"a", "b"}, f"兩個人坐在同一側：{spots}"

            # 會議中【交件了也不散會】：一個人講完話不代表會議結束。三個人交件時間本來就
            # 錯開，一交件就各自回位的話，白板前永遠只有一兩個人——看起來不像在開會。
            post(c, agent=main.KANBAN, kind="result", label="spawn_subagent", detail="我的意見")
            still = [n for n in main.occupied if main.occupied[n] in main.MEET_SPOTS]
            assert len(still) >= 1, "交件後不該立刻散會"

            # 而且交過件的人要【維持 busy】。生活迴圈是 `while id in busy: sleep()`——
            # 一被釋放它就接管，在 idle 間隔內隨機發一個 move_to，人就自己從會議室走掉。
            # 實測回報過：「有的 agent 會提前自行離開」。只驗「還在座位上」抓不到這個，
            # 因為釋放的當下他確實還在座位上，是【下一個 tick】才被帶走的。
            done_yet = [n for n in still if n in main.busy]
            assert len(done_yet) == len(still), \
                f"會議中交過件的人被放出 busy，生活迴圈會把他帶走：{set(still) - main.busy}"

            # 【板子一寫好就散會】——會議結束的時刻是「結論定案」，不是整個任務做完。
            # 先前綁在收工，畫面上會變成「板子都出來了、人還圍在白板前」，兩個投影各說各話。
            (wd / "board.json").write_text('{"task":"x","tasks":[]}', encoding="utf-8")
            post(c, agent=main.KANBAN, kind="think", label="")   # 隨便一則事件觸發檢查
            still = [n for n, s in main.occupied.items() if s in main.MEET_SPOTS]
            assert not still, f"板子出來了還有人圍在會議室：{still}"

            # 散會也要【解除 busy】。會議期間刻意把人留在 busy，解除的責任就落在散會這裡；
            # 漏掉的話他回到工位後再也不會被生活迴圈碰到、也不會被挑去支援——變成一尊雕像。
            assert not (main.busy - {main.KANBAN}), f"散會後還有人卡在 busy：{main.busy}"

            # 上板之後＝實作階段：新派的人改成各自回工位
            post(c, agent=main.KANBAN, kind="tool", label="spawn_subagent")
            third = [n for lst in main.sub_active.values() for n in lst][-1]
            assert main.occupied[third] == main.WORK_DESK[third], \
                f"上板後該回工位，實際去了 {main.occupied[third]}"
            # 散會要有人喊：收工時把還站在白板前的人請回位子，否則他們會一直杵在那裡
            # 直到生活迴圈下一輪隨機把人帶走——那段畫面同樣是錯的。
            (wd / "board.json").unlink()          # 回到會議狀態，讓還有人站在白板前
            post(c, agent=main.KANBAN, kind="tool", label="spawn_subagent")
            assert any(s in main.MEET_SPOTS for s in main.occupied.values())
            post(c, agent=main.KANBAN, kind="done", label="ok")
            left = [n for n, s in main.occupied.items() if s in main.MEET_SPOTS]
            assert not left, f"散會後還有人杵在白板前：{left}"
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
        main.CHANNELS_DIR = Path(tmp) / "channels"
        # ⚠ 一定要指定 COGITO_AGENTS_DIR：預設會從 CHANNELS_DIR 的【上一層】推導，而暫存目錄
        # 的上一層是系統 temp 根——跨測試共用，第二次跑就變成「已是最新」而回 0（當場踩到）。
        os.environ["COGITO_AGENTS_DIR"] = str(Path(tmp) / ".claw" / "agents")
        try:
            assert main.sync_agents() >= 1
            # 預設推導也要對：共享根的 .claw/agents/，不是頻道目錄——cogito 的 AgentLoader
            # 用的是 SkillsBaseDir=rootDir。寫錯地方檔案存在也永遠載不到（實際踩到：主持人
            # 被守則要求用人名，卻只能退回 implementer，因為人名檔它根本看不見）。
            del os.environ["COGITO_AGENTS_DIR"]
            assert main.agents_dir() == Path(tmp) / ".claw" / "agents", "預設推導錯了"
            os.environ["COGITO_AGENTS_DIR"] = str(Path(tmp) / ".claw" / "agents")
            d = main.agents_dir()
            names = {p.stem for p in d.glob("*.md")}
        finally:
            os.environ.pop("COGITO_AGENTS_DIR", None)
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

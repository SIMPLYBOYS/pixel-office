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


def recv(ws) -> dict:
    return json.loads(ws.receive_text())


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
            m = recv(ws)
            assert (m["agent_id"], m["text"]) == ("p01", "✓ 回報完成")
            assert "p01" not in main.busy
            assert any("委派" in x for x in main.agents["p01"].memory)
            r = c.get("/office/report/p01").json()  # 委派也有報告卡
            assert (r["task"], r["status"], r["report"]) == (
                "支援阿哲：code-reviewer", "ok", "LGTM，無阻塞問題")

            # 無名子 agent（探路者）：兩側正規化成空名，一樣開卡
            post(c, agent="p17", kind="tool", label="spawn_subagent")
            assert recv(ws)["agent_id"] == "p01"  # 又輪到有空的小美（move_to）
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
            post(c, agent="p17", kind="msg", label="謝謝，有需要再找我。")
            assert recv(ws)["text"] == "→ 回報"      # 只有回話泡，沒有「開工」也沒有 move_to
            post(c, agent="p17", kind="done", label="ok")
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
            # HITL 投影：走到老闆房門口站著等
            assert recv(ws) == {"agent_id": "p07", "action": "move_to", "target": "boss_1"}
            # 等審批＝球在別人手上：掏手機，不是站著像雕像（站著不動＝閒置，兩者不能同形）
            assert recv(ws) == {"agent_id": "p07", "action": "use", "target": "phone"}
            assert recv(ws)["text"] == "⚠ 等待審批"
            assert main.occupied["p07"] == "boss_1"
            assert "p07" in main.busy  # 等審批＝工作中，生活迴圈不得插隊蓋掉罰站走位
            r = c.get("/office/report/p07").json()
            assert r["approval"].startswith("⚠️ *高危操作審批請求*")
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
    print("✓ office 投影合約測試全過（含子 agent 映射、節流、失聯保險、頻道派工、人設同步）")


def previews() -> None:
    """產出預覽的三道界線：路徑不得越界、副檔名白名單、卡片外的目錄一律拒絕。
    這是唯一會把磁碟內容送出去的端點，破了就是任意檔案讀取。"""
    import base64, tempfile
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp) / "job"
        wd.mkdir()
        (wd / "a.md").write_text("# hi\n", encoding="utf-8")
        (wd / "k.env").write_text("TOKEN=x\n", encoding="utf-8")   # 不在白名單
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


if __name__ == "__main__":
    run()

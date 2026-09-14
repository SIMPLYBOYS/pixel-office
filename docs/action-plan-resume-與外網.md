# Action plan：任務「接著做」（resume）與 Pixel Office 外網存取

> 2026-09-14 討論整理，尚未實作。兩件事互不依賴，可分開做；順序建議先 A 後 B（A 小、B 有安全門檻）。
> 原則不變：投影誠實（resume 失敗就明講並退回從頭跑）、腳本是唯一真相、新斷言要驗紅。

---

## A. 中止或出錯的任務可以「接著做」

### 現況（已核實）

| 引擎 | 老闆派的活 | 班表任務 |
|---|---|---|
| CLI（Claude Code） | 橋用 `cli_session_id(aid, cwd)`（uuid5）發固定 id，第一次 `--session-id`、之後 `--resume`（`cli_session_args`）。中止後再說一句本來就是同一段對話 | 每次 `fresh=True` 開新 uuid4，id 記在卡上（`card["session"]`，🧾 回溯用）。中止後只能從頭重跑 |
| cogito | 每人一條長期 office session（`workspace/.sessions/office_<aid>-<hash>.json`），`/stop` 在回合邊界停，下一個任務接在同一段歷史後 | 同左 |

**實測（2026-09-14）**：拿老徐被橋重啟砍掉的卡 319（session `dc4e5038…`，紀錄尾巴是 tool_result 齊全的狀態），
在他的工作區以員工 profile 跑 `claude -p --resume <id> --max-turns 1`，一次成功，他準確回報做到哪、下一步是什麼。
一句回話的換算值約 $0.09（整段上下文都帶著）。**沒測到的形狀**：砍在工具呼叫發出、結果未回的瞬間（紀錄尾巴是懸空的 tool_use）。

### 要做的

1. **橋：`/office/dispatch` 收 `resume_card`**
   - 從 `history[aid]` 找那張卡；卡不是 `stopped`／`error` 就拒（「這張卡沒有中斷，直接派新任務」）。
   - CLI：`run_cli_task(..., resume=card["session"])` → argv 用 `--resume <id>`（不是 `cli_session_args`，那條是老闆固定對話）。
     訊息預設「接著把剛才沒做完的做完；先看工作區現況，不要重做已完成的部分。」＋老闆補的一句。
     session 檔不在（`CLI_SESSION_DIR.glob(f"*/{sid}.jsonl")` 找不到）→ 退回從頭跑，工作串明講「找不到上次的對話，從頭做」。
   - cogito：同一個 agent 送同樣的話即可（session 本來連著）；不需要 id。
   - 若那張卡是班表任務（卡上 `scheduled` 或 `sched_running` 留痕）：重新掛 `sched_running[aid] = {"job", "started": 現在}`，做完照常 `deliver_job`。
     `deliver_path` 用新的 started 看 mtime——檔案在接著做的過程一定會再被寫，成立。
   - 新開一張卡（不是重開舊卡）：卡上記 `resumed_from: <舊卡 id>`，稽核帳 `task.start` 帶 `resumed_from`。
2. **外殼：卡片動作列**
   - 狀態是 `stopped`／`error` 的卡多一顆「▶ 接著做」，旁邊一格可填修正（空白就用預設句）。
   - 用 DOM 事件綁定，不要把任何值拼進 `onclick` 字串（收件匣補跑那顆鈕踩過的坑，見對照筆記）。
   - 收件匣的「▶ 補跑」維持從頭跑；兩者並列，老闆選。
3. **保險：懸空 tool_use 的 resume**
   - 做之前先製造一條：起一個班表任務、在 WebFetch 發出後立刻砍行程，對那條 `--resume` 試一次。
   - 能接：照做。不能接（CLI 報錯或 API 400）：橋在 `--resume` 失敗（退出碼非 0 且 stderr 有 session／conversation 字樣）時自動退回從頭跑，並明講。
4. **測試（test_office.py）**
   - `cli_resume`：resume_card 對 stopped 卡 → argv 含 `--resume <卡上的 id>`、預設句＋老闆句進 stdin、新卡帶 `resumed_from`；對 ok 卡 → 拒。
   - session 檔不存在 → 走 `--session-id` 新 id、工作串有「從頭做」那行。
   - 班表卡接著做 → `sched_running` 重新掛上、收工有交付。
   - 每條新斷言拔掉對應邏輯驗紅。
5. **文件**：對照筆記加一節；README 的操作說明加「接著做 vs 補跑」一句。

### 驗收

- 中止老徐的趨勢任務 → 卡上按「接著做」→ 他從上次的進度續寫 `trend-<今天>.md` → 送到 Telegram／Slack；帳本看得到 `resumed_from`。
- 對一張正常完成的卡按不到這顆鈕。

---

## B. 從外網使用 Pixel Office

### 現況（已核實）

- 橋（8123）**沒有任何驗證**：`/office/*` 能連到就能派工、核准審批、讀員工工作區檔案（`/office/file`）、封存帳本。局域網單人用是刻意的，外網不行。
- WebGL 的 `BrainGateway.url` **寫死 `ws://localhost:8123/ws`**：從外網開頁面，畫布會去連瀏覽器自己的 localhost。
- 外殼與 WebGL 同源，`postMessage` 已檢查 `origin`；SSE（外殼）與 WebSocket（Unity）都要能穿過代理。
- cogito 的 8787、兩邊的 `.env`：**不對外**，只有橋出去。

### 路線（先 1 再 2；碼的改動兩條共用）

| | 1. Tailscale（私網） | 2. Cloudflare Tunnel ＋ Access（公網加登入） |
|---|---|---|
| 給誰 | 自己，從手機或另一台電腦 | 給別人看、要正常網址 |
| 開 port | 不用 | 不用（機器往外連） |
| 驗證 | 裝置身分（進得了 tailnet 才看得到） | 邊緣要求 Google／Email OTP |
| TLS | `tailscale serve` 自帶 | 自帶 |
| WS／SSE | 通 | 通（SSE 要 `Cache-Control: no-cache`，橋已帶） |
| 費用 | 個人免費 | 小團隊免費 |

### 要做的

1. **WebGL：ws 網址由頁面推導**
   - `Assets/Plugins/OfficeShell.jslib` 加一個 `GetBridgeWsUrl()`：`(location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws'`。
   - `BrainGateway.cs`：`#if UNITY_WEBGL && !UNITY_EDITOR` 用它覆蓋 `url`；編輯器與桌面版維持 inspector 的值。
   - 重建 WebGL（批次：`Unity -batchmode -quit -projectPath unity -executeMethod WebGLBuilder.Build`，見記憶 unity-batch-build）。
   - 驗：本機 `http://localhost:8123/shell/` 照常連上（`/office/status` 的 canvas = 1）。
2. **橋：共用密鑰（第二道門，邊緣驗證之外）**
   - `.env`：`OFFICE_WEB_TOKEN=`（空＝不啟用，局域網照舊）。
   - 啟用時：`/office/*`（含 `/ws`、`/events`、`/office/stream`）要帶 `X-Office-Token` 或 cookie `office_token`；沒帶或不對 → 401。
     `/shell/` 靜態檔放行（要能載入頁面才輸入密鑰）。
   - 外殼：401 時彈一格輸入，存 `localStorage.officeToken`，之後 `fetch` 統一帶 header；SSE（EventSource 不能帶 header）改成 `?token=` 查詢參數或先 `POST /office/login` 設 cookie——**選 cookie**：WebSocket 與 EventSource 都會自動帶。
   - Unity WebGL 的 `/ws`：瀏覽器同源 cookie 會帶上，不用改 C#。
   - 測試：`web_token`——沒設就全放行；設了：不帶 401、帶錯 401、cookie 對放行、`/shell/index.html` 不用帶；驗紅。
   - 密鑰不進工作串、不進帳本、不印。
3. **Tailscale（路線 1）**
   - 機器裝 Tailscale、登入；`tailscale serve --bg 8123`（HTTPS 反代到橋）。
   - 手機裝 Tailscale、同帳號；開 `https://<機器名>.<tailnet>.ts.net/shell/`。
   - 驗：手機能看到畫布動、能派工、審批卡能放行、SSE 不斷線 10 分鐘。
4. **Cloudflare Tunnel ＋ Access（路線 2，需要時再做）**
   - `cloudflared tunnel` 指到 `http://localhost:8123`，綁一個子網域。
   - Zero Trust → Access → Application：該網域，policy 只放行你的 Email（OTP）或 Google。
   - WebSocket 在 Cloudflare 預設開；SSE 不快取（橋已帶 `no-cache`）。
   - 驗：無痕視窗開網址 → 先被要求登入；登入後同上四項。
5. **不做／不開**
   - 不開 8787（cogito）；不開任何 `.env`；不用 ngrok 免費層（沒有登入層）；不用 Tailscale Funnel 給公網（沒有驗證）。
   - 不把審批鑰匙（`X-Approver-Token`）交給外殼——它仍留在橋的 `.env`（SoD，見 x402 回顧文件）。

### 驗收

- 關掉 Wi-Fi 用手機行動網路，開外網網址：畫布連得上、派一個任務給小美、審批卡出現並能放行、收工通知到手機。
- 沒帶密鑰或沒登入的人，看得到的只有 401（或登入頁）。
- 本機 `localhost:8123` 一切照舊。

---

## 待你決定

- A-3：懸空 tool_use 的 resume 若不能接，是「自動退回從頭跑」（建議）還是「這顆鈕只對正常中止的卡出現」。
- B：先 Tailscale（建議）還是直接 Cloudflare。
- B-2：密鑰要不要也擋 `/shell/` 靜態檔（擋了外殼就要用另一個登入頁；建議不擋，頁面本身沒有機密）。

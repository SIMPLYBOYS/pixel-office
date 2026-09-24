# Pixel Office

把 AI agent 的工作投影進一間像素辦公室：誰在做什麼、卡在哪、在等你批准什麼，
掃一眼就看得出來。員工有人設、有班表、有工作串與稽核帳本；高危操作會停下來等你放行
（HITL 審批），每個動畫與徽章都對應真實事件——**畫面上看到的都是真的發生過的事**。

三種引擎可以逐件事切換：cogito-agent、Claude Code CLI、Codex CLI。
舞台在 Unity，橋在 FastAPI，兩邊用 WebSocket 傳 JSON 指令。

## 影片導覽

<a href="docs/media/pixel-office-demo.mp4"><img src="docs/media/pixel-office-demo.gif" width="300" alt="Pixel Office 展示影片"></a>

26 秒直式展示影片（上面是無聲 GIF，點它看有音效的 mp4）。辦公室、名冊與任務看板都是實際跑起來的畫面錄影與截圖，不是動畫；
標「重建」的兩段是照外殼樣式重做的示意畫面（當下沒有真的協作板與待審批可拍）。

| 時間 | 畫面 | 對應功能 |
|------|------|----------|
| 0:03 | 員工起身走進會議室坐下 | `/cmd` 走位；會議室六個座位（`RoomBuilder.cs` 的 `meet_*`） |
| 0:06 | 四個人同時上工 | 班表 09:00 開工，多個 agent 並行；名冊的「工作中」狀態燈 |
| 0:10 | 全辦公室的任務，一張看板看完 | 外殼「📋 任務」分頁：進行中／等審批／完成／失敗・中止四欄 |
| 0:14 | 一件大事，整隊照相依推進（重建） | 名冊的「看板」協作模式：拆票、標出「⏳ 等誰」、最多 3 人並行 |
| 0:19 | 高危操作，停下來等你放行（重建） | HITL 審批卡＋頭上倒數徽章（徽章是辦公室實際使用的素材） |
| 0:23 | 三種引擎，逐件事切換 | cogito-agent／Claude Code／Codex CLI |

（原名 `unity_demo`；架構詳見 aaron-vault 的架構指南與建置筆記。）

```
unity/      Unity 6 專案（表現層：場景、NPC、尋路、台詞框）
backend/    FastAPI 橋（人設 + 記憶 + 派工 + 審批 + 班表 + 稽核帳本）
web/        網頁外殼（名冊、工作串、任務看板、指令頁）
tools/      素材管線（aseprite 設計圖拆解、角色幀抽取）
limezu/     LimeZu 授權素材與衍生物（不進 git，見下方「素材重建」）
```

## 日常啟動（兩個視窗）

**視窗 1 — 後端大腦：**

```bash
cd backend
.venv/bin/uvicorn main:app --port 8123
```

模式由 `backend/.env` 的 `OFFICE_MODE` 決定（`load_dotenv` 自動載入）：
- `OFFICE_MODE=projection`（預設建議）：生活大腦（Claude 決策閒逛/搭話）停用，NPC 平時
  零成本 idle（偶爾隨機走動），只有 cogito 的 `/office/event` 工作事件驅動行為——
  接真工作用這個，不燒 API、狀態畫面也不被閒逛污染。
- 註解掉該行＝生活模擬 demo 模式（Claude 決策過日子）。
- 判準：啟動連上 Unity 後印「啟動 N 個 agent（純投影…）」即生效；改 `.env` 後要重啟 uvicorn。

**員工與人設**：一個人兩個檔——`backend/personas/pXX.yaml`（名字／職務／團隊／個性）與
`pXX.md`（角色設定，外殼點名冊就看得到）。加人只要多這兩個檔 + `CharacterBuilder.cs` 補一行
出生點 + Unity **Tools → Build Characters**。

設了 `COGITO_CHANNELS=<cogito>/workspace/channels` 時，橋啟動會把 `pXX.md` 同步成各頻道的
`AGENTS.md` **與** `CLAUDE.md`，內容同源：cogito 的 PromptComposer 讀前者、Claude Code（CLI 引擎）讀後者，
人設才真的影響行為。CLI **不讀 AGENTS.md**（2026-09-07 實測），所以兩個檔名缺一不可。
⚠️ 覆寫保護：只有「檔案不存在」或「開頭是 `<!-- office-persona:` 標記」才會寫——手寫的
一律保留並印警告。想自己維護某個頻道的檔案，把那兩行標記刪掉即可。

**班表與交付**：`backend/schedule.json` 是辦公室的例行任務（格式見 `schedule.json.example`）：`hour` 必填、
`weekday` 省略＝每天、`engine` 逐件事選 cogito／cli、`repo` 綁工作 repo。到點走一般派工路徑，走位、工作串、
報告卡全部照常。job 帶 `"deliver": {"file": "trend-{date}.md"}` 時，收工後把那個檔送到 `OFFICE_DELIVER_TO`
（`telegram:<chat_id>,slack:<channel_id>`，與 cogito 的 `COGITO_CRON_NOTIFY` 同格式），老闆不在辦公室也看得到。
沒檔、檔沒更新、API 回錯，工作串都會明講；送到了才寫「已送到」。

全辦公室共用的守則另有一份來源 `backend/personas/office.md`，啟動時同步到兩個引擎各自的「全員」座位：
cogito 共享根 `workspace/AGENTS.md`（PromptComposer 先讀根、再疊頻道那份）與員工 CLI profile 的
`$CLAUDE_CONFIG_DIR/CLAUDE.md`（Claude Code 的使用者層指示，實測會載入）。同一套覆寫保護。

泡泡中文字型（一次性）：`tools/get_font.sh` 取得 Noto Sans CJK TC（OFL 授權，16MB 已
gitignore）放到 `unity/Assets/Resources/OfficeFont.otf`。編輯器匯入時只會把泡泡用字烘成
圖集（見 `OfficeFontImporter.cs` 的字表），build 不會被字型拖胖；沒有這個檔案時 WebGL
的中文泡泡會是空白（Unity 內建 Arial 無中文字形）。**改泡泡用詞要同步更新那份字表。**

名冊頭像（可選，一次性）：`python3 tools/make_avatars.py` 從角色圖抽 64×64 像素頭像到
`backend/avatars/`（LimeZu 衍生物，已 gitignore；沒跑就顯示文字頭像）。分組看 persona 的
`team` 欄位，沒填歸「未分組」。

看報告：Play 中**滑鼠點任一 NPC** 彈出他最近一次任務的報告卡（任務、狀態、報告全文；
Esc 或點空白處關閉）。資料來自橋的 `GET /office/report/{id}`；深挖 artifacts 請開 claw-dashboard。

## 網頁版（Web 外殼，Pixffice 式佈局）

1. Unity 選單 **Tools → Build WebGL (辦公室網頁版)**（一次即可，改場景才需重建；產物在
   `unity/Builds/WebGL`，已 gitignore）
2. 起後端後瀏覽器開 **http://localhost:8123/shell/** ——左側員工名冊（狀態燈）、中間像素
   辦公室（WebGL）、右側工作串（時間軸即時滾動＋報告全文）
3. 沒建置 WebGL 也能用：中間顯示提示，名冊和工作串照常運作（資料同源 `/agents`、`/office/report`）

產出檔案在哪：每個頻道有獨立工作目錄（cogito 的 `workspace/channels/<平台>_<頻道>`），
從網頁派給阿哲＝`office_p17`。任務卡上會標「📁 channels/office_p17」（滑過看完整路徑）。

**從網頁派工**：點左側員工 → 下方輸入框描述任務 → Ctrl+Enter 交辦。前提是 cogito bot
開了 HTTP 入口（cogito `.env`：`COGITO_HTTP_ADDR` + `COGITO_HTTP_TOKEN`、`office-web` 列入
`COGITO_ALLOWED_USERS`；橋 `.env`：`COGITO_HTTP` + 同值 token）。高危操作會在右欄跳
**審批卡**（核准/駁回按鈕），逾時自動拒絕。名冊不依賴 Unity——沒開 Unity 也能派工看進度，
Unity 只是渲染面。

派工兩條路（可並用）：
- **一次性（CLI）**：`claw-cli -office http://localhost:8123 -office-agent p17 -dir . -prompt "..."`
- **常駐（Slack/Telegram bot）**：cogito bot 啟動時設 `COGITO_OFFICE_URL=http://localhost:8123`，
  頻道派的任務自動投影——未知頻道 id 由橋動態指派閒置 NPC（黏性：同頻道固定同員工）。

啟動時若印出「⚠ 未設定 ANTHROPIC_API_KEY」代表 `.env` 沒讀到（見下方一次性設定）——
此模式 NPC 仍會動，但退化成隨機走動。

**視窗 2 — Unity：**

1. Unity Hub 開啟專案 `unity/`（不是 repo 根目錄！）
2. 開場景 `Assets/Scenes/SampleScene`
3. 按 **Play**

先開哪個都行（Unity 每 3 秒重試連線）。連上的判準：
- Unity Console：「BrainGateway: 已連上後端，假大腦停用」
- 後端終端機：「✓ Unity 已連線」→「啟動 3 個 agent（Claude 決策）」

之後就看戲：後端終端機滾動每個人的決策，`💬` 開頭是對話迴圈。

## 一次性設定（新機器）

1. **Unity 6000.5.3f1**（Unity Hub → Add project from disk → 選 `unity/`）。
   首次開啟會重建 Library 快取＋抓 NativeWebSocket 套件（需要網路與 git），等它跑完。
2. **後端環境**：
   ```bash
   cd backend
   uv venv .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   cp .env.example .env   # 填入 ANTHROPIC_API_KEY=sk-ant-...
   ```
3. **素材重建**（僅 fresh clone 需要——limezu/ 不進版控）：
   - 到 itch.io 購買 LimeZu《Modern Interiors 完整版》與《Modern Office》，解壓到 `limezu/`
   - **素材授權與 credit**：像素素材 © [LimeZu](https://limezu.itch.io)（Modern Interiors／Modern Office），授權可用於任何專案、**不可散布素材本身**——因此 `limezu/` 與 `unity/Assets/Sprites/LimeZu/` 一律不進版控，fresh clone 沒有它們也能跑橋與外殼（辦公室 3D 畫面需自行購買後重生）。Modern Interiors 要求標示 credit：limezu.itch.io
   - 跑管線重生素材，複製產物進 Unity：
     ```bash
     python3 tools/extract_design.py limezu/Modern_Office_Revamped_v1.2/6_Office_Designs/Office_Design_2.aseprite
     for n in 17 1 7 5 12 8 19; do python3 tools/make_character.py $n; done   # 七位員工（含一次性動作列）
     python3 tools/make_emotes.py                                              # 頭邊的狀態徽章
     # 產物在 limezu/_extracted/，複製到 unity/Assets/Sprites/LimeZu/
     # （Design/、Characters/p*/、Emotes/）
     python3 tools/trim_props.py    # ⚠ 複製【之後】才跑：它直接改 Unity 底下的 Design/
     ```
   - `trim_props.py` 把下排長桌 `obj_24` 裁短。抽取工具依「相連像素」切元件，把第 4 組
     工作站的桌面與桌角盆栽併進長桌同一張圖；拿掉最右一組（RoomBuilder 的 `Hidden`）時
     桌子必須跟著縮短，否則留下沒收邊的切口。**漏跑這步 = 右側走道會被長回來的桌子擋住。**
   - Unity 選單 **Tools → Build Room**、**Tools → Build Characters** 重建場景

## 觀察與手動介入

```bash
curl localhost:8123/agents     # 每個人的位置、記憶、是否聊天中
curl localhost:8123/events     # 最近事件（arrived 等）
# 跳過大腦直接下指令：
curl -X POST localhost:8123/cmd -H 'Content-Type: application/json' \
     -d '{"agent_id":"p17","action":"move_to","target":"cooler_1"}'
# 直接擺一個姿勢（驗收動畫最快的方式，不必等 agent 真的跑）：
curl -X POST localhost:8123/cmd -H 'Content-Type: application/json' \
     -d '{"agent_id":"p05","action":"use","target":"hurt_down"}'
```

姿勢名稱：`sit_up`／`sit_left`／`sit_right`、`phone`、`sleep`、`book`、
`face_*`、`gift_*`、`hurt_*`（`pick_up_*`／`lift_*`／`throw_*` 已在 prefab 裡，還沒接事件）。
`hurt_*` 是一次性動作——**演一秒就自己退掉**，眨眼會錯過；截圖驗收要連續重送才拍得到。

頭邊的徽章（掃一眼就知道誰動不了）。一次只掛一個，**由上而下就是優先序**——
上面的成立時下面的不顯示：

| 徽章 | 意思 | 怎麼消掉 |
|------|------|----------|
| 倒數餅圖（綠→黃→橘→紅） | 在等你核准／駁回，餅愈滿愈接近逾時自動拒絕 | 外殼上按放行或駁回 |
| 藍問號 | 同上，但**算不出期限**時的退路（不知道剩多久就不畫倒數） | 同上 |
| 紅驚嘆號 | 訂閱額度被擋住了 | 等額度恢復；有工具事件就消 |
| 黃驚嘆號 | 額度快滿（過 90%） | 收工自動消 |
| 空白思考泡 | 卡住空轉，人已走去飲水機 | 有工具事件就消 |
| 黃寶石 | 有待審的記憶提案（他學到的東西沒人收） | 點名冊上的 💡 展開清單，逐條放行或丟棄 |

額度那兩個目前**只有 CLI 引擎會亮**——cogito 在 provider 層自己重試 429 就吞掉了，
沒有送進 office 協定。

同一份狀態也會出現在左側名冊列（`want_emote()` 同源）。名冊多兩個好處：
看板沒有身體、頭上掛不了徽章，那 33 條只有名冊看得到；而且**沒建 WebGL 時名冊照常運作**。
提案數在名冊是**獨立顯示**的——頭上一次只掛得下一件事，名冊有位子並排，
所以「他在等審批」跟「他還有 6 條沒人收」可以同時看到。

三種運行模式：
| 模式 | 條件 | 行為 |
|------|------|------|
| 真大腦 | 後端開 + API key | Claude 決策、人設台詞、對話迴圈 |
| 合約測試 | 後端開、無 key | 隨機走動（驗管線用）|
| 離線 | 後端沒開 | Unity 假大腦自主亂走 + 相遇「...」泡泡 |

後端可隨時開關——假大腦與真大腦會自動熱切換。

## 常用調校

- 節奏：`backend/main.py` 的 `DECISION_INTERVAL`（決策間隔）
- 成本：`backend/agent.py` 的 `MODEL`（換 `claude-haiku-4-5` 省 80%）
- 人設：`backend/personas/*.yaml`（改完重啟後端即生效）
- 對話上限：`main.py` 的 `MAX_ROUNDS`

## 疑難排解

| 症狀 | 原因與解法 |
|------|-----------|
| `address already in use` | 8123 被舊的 uvicorn 佔住，先 Ctrl+C 或 `lsof -i :8123` 找出來 |
| Unity 一直沒連上 | 場景裡要有 `BrainGateway` 物件（沒有就 Tools → Build Characters）；Play 模式才會連 |
| NPC 全部站著不動 | 後端 log 看是否有 429/decide 失敗；無 key 模式看是否印出隨機走動 |
| 編譯錯誤 | 真正的錯誤訊息在 `unity/Logs/Editor.log`（不是 ~/Library 那份）|
| Hub 開錯資料夾 | 專案是 `unity/`，開到 repo 根會生出一個空專案（gitignore 已防護）|

## 授權

程式碼是 MIT（見 `LICENSE`），**只涵蓋 Pixel Office 自己寫的程式碼**。

像素素材 © [LimeZu](https://limezu.itch.io)（Modern Interiors／Modern Office）。
素材**不包含在這個 repository 或任何 release 裡**，也不能被 MIT 重新授權——
要跑辦公室畫面請自行到 itch.io 取得合法副本，再依上面的「素材重建」重生。
Modern Interiors 的授權要求標示 credit：limezu.itch.io。

`docs/media/` 的展示影片（mp4 與 GIF）是辦公室實際畫面的錄影與截圖，畫面裡看得到 LimeZu 素材，
僅用於展示本專案。它們不是素材檔，拿不回原始圖塊，也**不在 MIT 授權範圍內**。

第三方來源與授權邊界整理在 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

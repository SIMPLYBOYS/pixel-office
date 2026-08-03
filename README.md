# AI 辦公室模擬（Unity × Claude 多 Agent Demo）

像素辦公室裡三個有人設的 NPC，由 Claude 決策過日子：走動、入座、喝水、
頭上冒中文台詞、互相搭話對談。舞台在 Unity，大腦在 FastAPI 後端，
兩邊用 WebSocket 傳 JSON 指令（架構詳見 aaron-vault 的架構指南與建置筆記）。

```
unity/      Unity 6 專案（表現層：場景、NPC、尋路、台詞框）
backend/    FastAPI 大腦（persona + 記憶 + Claude 決策 + 對話迴圈）
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
`AGENTS.md`（cogito 的 PromptComposer 會讀進系統提示，人設才真的影響行為）。
⚠️ 覆寫保護：只有「檔案不存在」或「開頭是 `<!-- office-persona:` 標記」才會寫——手寫的
`AGENTS.md` 一律保留並印警告。想自己維護某個頻道的檔案，把那兩行標記刪掉即可。

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
   - 跑管線重生素材，複製產物進 Unity：
     ```bash
     python3 tools/extract_design.py limezu/Modern_Office_Revamped_v1.2/6_Office_Designs/Office_Design_2.aseprite
     for n in 17 1 7 5 12 19; do python3 tools/make_character.py $n; done   # 六位員工
     # 產物在 limezu/_extracted/，複製到 unity/Assets/Sprites/LimeZu/（Design/ 與 Characters/p*/）
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
```

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

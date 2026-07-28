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
.venv/bin/uvicorn main:app --port 8123                        # 生活模擬 demo 模式
OFFICE_MODE=projection .venv/bin/uvicorn main:app --port 8123  # 純投影模式（接 cogito 真工作用）
```

`OFFICE_MODE=projection`：生活大腦（Claude 決策閒逛/搭話）停用，NPC 平時零成本 idle
（偶爾隨機走動），只有 cogito 的 `/office/event` 工作事件驅動行為——接真工作時用這個，
不燒 API、狀態畫面也不被閒逛污染。

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
     python3 tools/make_character.py 17 && python3 tools/make_character.py 1 && python3 tools/make_character.py 7
     # 產物在 limezu/_extracted/，複製到 unity/Assets/Sprites/LimeZu/（Design/ 與 Characters/p*/）
     ```
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

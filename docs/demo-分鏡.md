# 請購單 for Agents — demo 分鏡

> 兩個版本共用同一套鏡頭：**2 分鐘影片**（必繳、Round 1 書審主要依據）與 **3 分鐘現場**（Round 2 若入選）。
> 現場版＝影片版每段多留 15–20 秒讓評審看清楚。所有畫面都是真跑的：`tools/demo_stack.sh` 拉起三行程，
> 只換 `PRICE` 就切段。⛔ 不用投影片演系統；投影片只放開場對照組與 what's next。

tagline（每個版本開場十秒後、報名字時講）：
**「Agent 是第一種 commit 那一刻沒有人的公司支出。誰批准、憑哪條規則、留不留得下證據。」**

## 時間軸（影片 2:00）

| 時間 | 段 | 畫面 | 旁白（一句） |
|---|---|---|---|
| 0:00–0:10 | 對照組 | 投影片一張：五筆支出、月底一個總數、歸因欄全空 | 「今天 agent 花錢長這樣：月底一張帳單，不知道為了哪個 task。」 |
| 0:10–0:20 | 報名字 | 切到辦公室全景，字卡：請購單 for Agents ＋ tagline | 講 tagline |
| 0:20–0:50 | **段 1・Ask** | `PRICE=1.00`。老徐坐工位 → 工作串跳出 `▸ request_payment` → 審批卡**長成請購單**（任務／商家／金額／資源／裁決）→ 老徐走到老闆房門口掏手機、頭上倒數餅圖 → 按「放行」→ 工作串 `✅ 已結算 tx: 0xmock…` → 稽核面板多一列 ALLOW／人 | 「agent 只能開請購單。金額超過免審額度，單子送到老闆桌上——注意卡上是 policy 要簽的**確切參數**，不是模型的理由。核准後由出納簽名，agent 從頭到尾碰不到金鑰。」 |
| 0:50–1:10 | **段 2・Deny** | `PRICE=9.99`（或商家改成不在白名單的 host）。同一個動作 → 工作串 `✗ request_payment 政策拒絕：單筆 $9.99 超過上限` → 稽核面板紅字 DENY／budget，**沒有人被打斷** | 「超過上限直接退件，附規則與理由。**被拒的也落帳**——這是攻擊偵測的證據，facilitator 看不到這筆，只有 policy 層有。」 |
| 1:10–1:35 | **段 3・誰替 AI 簽字** | 外殼按「放行」但橋**沒有**審批鑰匙 → 工作串出現 cogito 的 `🚫 只有管理員可以 approve/reject` → 加上 `COGITO_HTTP_APPROVER_TOKEN` 再按 → 過 | 「這是我們自己 README 記了半年的洞：派工的那把鑰匙原本連帶拿到審批權，agent 提單、橋核准、迴路裡沒有人。現在派工與審批是兩把鑰匙，各開一扇門。」 |
| 1:35–1:50 | 稽核 | 拉近稽核面板：ALLOW／DENY／ASK 三列並排，每列有規則、放行者、tx | 「每一張單：誰提、為了哪個 task、哪條規則、誰批、簽了什麼、結算成什麼。」 |
| 1:50–2:00 | 收尾 | 投影片：堆疊圖（Settler 可換 ／ Policy 純函式 ／ Intent 是單位）＋ what's next 三行 | 「x402 解 handshake；我們做的是它明說 out of scope 的那三件——custody、budget、policy。」 |

## 段落切換怎麼做（錄影當天）

```bash
PRICE=1.00 tools/demo_stack.sh     # 段 1：Ask → 老闆房門口
# 派工給老徐：「這個研究任務需要 http://127.0.0.1:4021/premium-data 的資料，請用 request_payment 取得並摘要內容。」
# Ctrl-C，換價格
PRICE=9.99 tools/demo_stack.sh     # 段 2：Deny
# 段 3 不用重啟：先把橋 .env 的 COGITO_HTTP_APPROVER_TOKEN 拿掉重啟橋按一次（被拒），加回去再按一次（過）
```

錄影前把 `PRICE=1.00` 先跑一次暖機（第一次 `go run` 要編譯，不要讓觀眾等）。

## 三筆靜默、第四筆才打斷（§0.2 設計 3，選配）

若時間夠，段 1 前面先用 `PRICE=0.05` 派三次：三筆 Allow 靜默蓋章、辦公室安靜、稽核面板多三列綠的——
然後才切 `PRICE=1.00` 讓第四筆走到門口。旁白：「人不看每一筆，人只被例外打斷。」影片版若時間不夠就砍。

## 誠實邊界（評審追問時的答法，不主動講）

- **結算是 mock**：本地 402 server ＋ facilitator（RESERVE／nonce／verify-settle 分開）。真上鏈是 SDK 一個 wrapper；演講者自己說價值不在那層。
- **金鑰三層**只做到兩層（出納 ＋ 權限分離），沒有可撤銷的 session key。
- **approver 記的是 "admin"** 不是人名；`done` 事件不帶 tx（回執在工具結果裡）。
- **`upto` 的用量爭議**開放，演講者也沒解。

## what's next（收尾投影片三行）

1. 真 facilitator（Base testnet）— Settler 換實作，policy 不動
2. A2A：NPC 付 NPC — 同一張請購單，payer 是另一個 agent；辦公室場景原生
3. 同一個 gate 掛到 Claude Code（MCP）— policy 層不綁 harness

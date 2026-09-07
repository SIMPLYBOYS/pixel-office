# 對照筆記：FUTUREMODE 2026 的「真實數字員工」——每個員工每天有例行任務

> 來源：Aaron 聽完 FUTUREMODE 2026 議程與 workshop 後的觀察（2026-09-07）。
> 場上反覆出現的形狀是：**數字員工不是等人派工的工人，是有班表的同事**——每天固定時間
> 整理 GitHub trend、彙整 Threads 上的科技話題、巡一輪 repo，產出落在固定地方，第二天接著做。
> 這份筆記回答一件事：**以目前的 repo 狀態，cogito 與 CLI wrapper 兩條路徑分別離這個形狀多遠。**
> 判斷全部建立在讀過的實作上（檔案座標見各節），不是想像。

## 根本差異：隨叫隨到 vs 有班表

Pixffice 現在的員工是**恆久物**（常駐、有人設、有記憶、有工作區），但工作**全部由老闆派**。
「真實數字員工」多的那一件是**主動性來自制度而不是來自人**：時間到了他自己開工。
這跟辦公室中心的敘事完全相容——班表是辦公室的制度，不是大腦的排程（Devin 對照筆記 ③ 的立場）。

## 一、現況：排程器已經存在，缺的不是它

橋在 `c486874` 就有一張**班表**（`backend/schedule.json`，範例 `schedule.json.example`；
迴圈 `schedule_loop` → `run_due_jobs` → `office_dispatch`）。到點走**一般派工路徑**，所以：

- 走位、工作串、報告卡、工作區檔案全部免費——跟老闆派的任務長一樣
- `office_dispatch` 按人設的 `engine` 欄位分流，**同一張班表兩個引擎都能跑**
- 防重跨重啟持久化（`sched_last`）；人在忙這輪跳過並留痕，不排隊

所以 cogito 與 CLI 的差別**不在觸發**，在**派下去之後誰執行**。

### 兩條執行路徑對照

| | cogito（`office_dispatch` → cogito `/task`） | CLI wrapper（`run_cli_task` → `claude -p`） |
|---|---|---|
| 向外查資料 | `web_search`＋`fetch_url`，走 Tavily（`internal/tools/web_search.go`）；沒設 `TAVILY_API_KEY` 兩顆都不註冊。bash 是 host executor，可跑 `gh`／`curl` | Claude Code 內建 WebSearch／WebFetch／Bash，零設定 |
| 沒人在場撞到高危操作 | 審批卡發到辦公室，沒人按就等到逾時自動拒絕（cogito `cmd/claw/cron.go` 自己的註解稱這是「碰巧安全」） | `acceptEdits`（`OFFICE_CLI_PERMISSION`）直接拒，不卡 |
| 跟昨天比、累積觀點 | 頻道 session `office_<aid>` 累積＋記憶提案／`recall` | 固定 `--resume` session 累積（`cli_session_id` 綁 aid × cwd），會越滾越長 |
| 成本 | API 計費；人設可指定便宜模型 | 吃訂閱額度（`rate_limit_event`），零 API 費 |
| 人設 | AGENTS.md 同步在工作區 | 同一份，Claude Code 自己會讀 |
| 交付 | 報告卡＋工作串＋`/office/wsfile` | 同一條投影（`office_event`） |

### 明確不走的兩條

cogito 另有**內建 cron**（`.claw/cron.json`，`internal/cron`）與 **`claw-cli`＋OS crontab**。
兩條都**不適合數字員工**：內建 cron 跑在獨立 session `cron-<id>`、用 `TerminalReporter`、每次執行前
`sess.Reset()`——辦公室完全看不到、也不累積；OS crontab 直叫 `claude -p` 同理。
它們是「機器上跑一個腳本」的形狀，不是「同事在辦公室裡開工」。班表留在橋這一層。

## 二、行動清單（依「解真實缺口 × 成本」排序）

### ① 班表支援「每天」—— ⬜ 待做

`run_due_jobs` 只認 `weekday`＋`hour`：`job.get("weekday") != now.tm_wday`，欄位缺＝`None`＝永遠不命中，
「每天」現在要寫七條。改成 **weekday 省略即每天**（`not in (None, now.tm_wday)`），一行。
順手把 `schedule.json.example` 加一條每天的 GitHub trend 範例。
驗紅：一條沒有 weekday 的 job 在改前不觸發、改後觸發。

### ② 資料來源要誠實 —— ⬜ 待做（先做 GitHub，Threads 不承諾）

- **GitHub trending 沒有官方 API。** 可行：`gh search repos --sort stars --created ">$(date -v-1d +%F)"`
  （bash，兩個引擎都有）或抓 `github.com/trending` 頁（cogito 走 `fetch_url`／CLI 走 WebFetch）。
  兩者都不是「官方 trending」，任務文字要講清楚產出的是哪一種。
- **Threads 沒有公開的讀取 API。** 官方 Threads API 只給自己帳號的貼文與 insights；
  「Threads 上的主流科技話題」目前只能靠搜尋引擎間接（`site:threads.net`），品質不會好，
  兩條路徑一樣。**不在班表上承諾這條**，等有可用資料源再說；投影誠實同樣適用於資料源。

### ③ 班表任務標成無人值守 —— ⬜ 待做（cogito 端）

班表派工目前對 cogito 而言是**有人的頻道**：Ask 會等一個可能不會來的人，逾時才拒。
兩個選項：（a）cogito `/task` 多帶 `unattended` 欄位，橋在班表派工時帶上，cogito 走
`policy.WithUnattended`；（b）接受「例行任務不碰需審批的工具」——整理趨勢本來就只有查與寫檔，
實務上撞不到。**先選 (b)，撞到再做 (a)**：(a) 是 cogito 端的介面變更，不該為還沒發生的事先動。

### ④ 產物歸檔、隔天接著做 —— ⬜ 待做（只是任務文字）

每天一份 `trend-<日期>.md` 落工作區，任務文字要求**先讀昨天那份再寫今天的**（新出現／掉出／連續幾天）。
「跟昨天比」靠檔案最穩——兩個引擎的記憶機制（cogito 的頻道 session、CLI 的 `--resume`）都只是加分，
且 CLI 那條 session 會無限增長，不該把連續性押在它上面。不需要改程式。

## 三、引擎怎麼選

觸發統一用橋的班表；**執行引擎按員工選**（人設 `engine` 欄位）：

- 省錢、要免設定的抓網頁 → **CLI**
- 要審批、記憶蒸餾、Slack 推播、政策裁決 → **cogito**

第一步只做 ①＋一條 GitHub trend 範例班表，跑幾天看實際產出，再決定 ②③ 的深度。

## 四、一句話總結

排程器早就在，缺的是「每天」這個粒度與誠實的資料源；別為了數字員工去用 cogito 的內建 cron——那條辦公室看不見。

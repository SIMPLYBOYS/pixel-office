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
| 人設 | 工作區 AGENTS.md（PromptComposer 讀） | 工作區 CLAUDE.md（Claude Code **不讀 AGENTS.md**，實測；修前 CLI 員工全是無人設） |
| 交付 | 報告卡＋工作串＋`/office/wsfile` | 同一條投影（`office_event`） |

### 明確不走的兩條

cogito 另有**內建 cron**（`.claw/cron.json`，`internal/cron`）與 **`claw-cli`＋OS crontab**。
兩條都**不適合數字員工**：內建 cron 跑在獨立 session `cron-<id>`、用 `TerminalReporter`、每次執行前
`sess.Reset()`——辦公室完全看不到、也不累積；OS crontab 直叫 `claude -p` 同理。
它們是「機器上跑一個腳本」的形狀，不是「同事在辦公室裡開工」。班表留在橋這一層。

## 二、行動清單（依「解真實缺口 × 成本」排序）

### ① 班表支援「每天」—— ✅ 已完成（2026-09-07）

`run_due_jobs` 原本只認 `weekday`＋`hour`：欄位缺＝`None`＝永遠不命中，「每天」要寫七條。
改成 **weekday 省略即每天**（`not in (None, now.tm_wday)`），一行；測試加一條沒有 weekday 的任務，
驗過改前紅（`['例行巡檢']` 少了它）、改後綠。

**第一張真班表**：老徐（p19）每天 09:00 整理 GitHub 趨勢，`backend/schedule.json`（範例同步在 example），
**走 CLI**（job 的 `engine: cli`）。engine 是這件例行事的屬性不是這位員工的：省錢的例行事走 CLI、要審批的走
cogito，同一個人可以兩種都有；所以班表派工帶 `scheduled` 標記，dispatch 不把它記成「外殼最後選的引擎」
（測試斷言 `engine_sent` 不動）。

**CLI 在 `-p`＋`acceptEdits` 下實測（2026-09-07，haiku，各一次）**：
- Bash 跑 `date`：直接執行、零拒絕，不需要 `--allowedTools`
- WebFetch 抓 `github.com/trending`：直接執行、回了三個真 repo
- Bash 跑 `gh search`：**被 CLI 自己的沙箱擋掉網路**（回「沙箱阻止了對 GitHub API 的網路訪問」）

所以任務文字兩條資料源**都走 WebFetch**（trending 頁＋ `api.github.com/search/repositories`），明講不要用
`gh`／`curl`。要讓 Bash 有網路是使用者在 `/sandbox` 裡放行 `api.github.com` 的決定，不在任務文字裡繞。

任務文字把 ②④ 的要求直接寫進去：先讀昨天的 `trend-<日期>.md`、報告開頭講清楚都不是官方榜、抓不到就寫
抓不到、只讀不改不 push。產物是 md 不是 html：員工隔天要**讀**它、老闆要 diff 它，md 兩件都順。
CLI 吃訂閱額度、模型跟人設（Opus）；cwd 是老徐的頻道工作區，報告落在那裡，外殼工作區檔案面板打得開。

**試跑（2026-09-07 13:16，hour 暫改成當下小時）**：班表一分鐘內點到老徐、走位與工作串照常、trending 頁抓到。
但交付物沒落地，卡片卻標「✔ 任務完成」。根因**不在模型、不在班表**：

- `~/.claude/settings.json` 把 `Write`／`Edit`／`git commit`／`git push` 放在 `permissions.ask`。ask 規則壓過
  acceptEdits 模式、也壓過 `--allowedTools Write`（兩個目錄各測一次）。`-p` 沒有人能回答 ask ＝ 拒絕。
  翻遍 p01／p05／p07／p19 的 CLI transcript：**CLI 路徑從來沒有成功寫過任何檔案**，這是第一個以檔案為交付物的任務才浮出來。
- `api.github.com` 的 WebFetch 放行寫在 `~/.claude/settings.local.json`，Claude Code 不讀那個位置。
- 全域 hook 指的 `check_main_branch.py` 哪個專案都沒有；每次 Write 留一筆非阻斷失敗（exit 127），無害但是死設定。

**決定：員工用獨立的 Claude Code profile**（`CLAUDE_CONFIG_DIR=~/.claude-office`，橋 `.env` 設、子行程繼承）。
員工是無人值守的 -p 行程，權限姿態本來就不該跟老闆本人互動用的一樣：那裡只有 allow（Write／Edit／WebSearch／
github 兩個 domain）與 deny（rm／git clean／git push／publish／讀 .env 與 ssh），**沒有 ask**——要問的事在無人值守下
等於拒絕，寧可明講。代價：該 profile 要登入一次；員工的 CLI session 收在它底下，等於從新對話開始。

**換 profile 後重跑（同日 13:36，班表觸發、CLI、Opus）**：兩條來源都抓到，還自己對前四名打 `api.github.com/repos` 核對星數，寫出 `trend-2026-09-07.md`（85 行）。報告開頭先講資料怎麼來、哪條是新建榜不是趨勢榜、哪些是刷星噪音——正是任務文字要的誠實。唯一被擋的是第一條複合 Bash（沙箱要求拆開），CLI 自己拆成三條重來。

**已修（同日）**：CLI 的 result 帶 `permission_denials`（`[{tool_name, tool_use_id, tool_input}]`），橋先前忽略它。現在 `cli_done_events`：檔案類工具（Write／Edit）被擋 → 卡標 error、工作串多一行「⛔ 交付被權限擋下：Write×1」；其他工具被擋 → 維持 CLI 的判斷但留一行 ⚠（卡 234 的複合 Bash 就是這種，它自己拆開重來了）。測試用實抓的 result 形狀，驗過舊邏輯（只看 is_error）在 Write 被擋時標成 ok 而紅。卡 233 本身留在歷史裡不改——它是這條規則的來源。

**同日第二輪調整（六項）**：

1. 根 `workspace/AGENTS.md` 那份 6 月 demo 指南（「本專案以 Go 撰寫」「API 回傳含 code/message」）換成辦公室共通守則
   （誠實／工作方式／邊界），來源 `backend/personas/office.md`；舊檔備份在 scratchpad。
2. 共通守則同步到兩個引擎的全員座位：cogito 根 `AGENTS.md`（composer 先根後頻道，cogito `41af31f`）、
   員工 CLI profile 的 `~/.claude-office/CLAUDE.md`（探針實測：cwd 無指示檔時仍逐字列得出三個小節）。同一套手寫保護。
3. 老徐 26 條記憶歸檔 2 條（`memory-archive/`，可復原）：「API 回應必須統一格式」「後端用 Go」——兩條都是從那份 demo 指南推出來的。
   其餘 24 條沒動；零命中是 recall 索引的問題，不是內容的問題。
4. 兩個行程要重啟才吃到新碼（橋：cli_done_events、面板過濾、班表新 session、能力面板；cogito：composer 疊頻道 AGENTS.md）。
5. 班表派的 CLI 任務每次開新 session（`fresh`）：例行事靠工作區檔案接續，不靠對話；老闆派的活照舊接回上一次。
6. 能力面板帶 `?agent=`：走 CLI 的人看 CLI 回報的清單，其他人與不帶人＝cogito 的；CLI 沒回報過就明說，不拿 cogito 的清單充數。
   外殼換人時跟著刷，標題標出是誰的。cogito 關著時問走 cogito 的人：明講「入口連不上，現在派給他會失敗」，
   不再退到 CLI 的清單充數（那是全員面板的退路，按人問時是誤導——Aaron 實測點老徐看到 250 個 CLI 工具，他一個都用不上）。

**交付到遠端（同日）**：Aaron 要在辦公室外看到產出。班表 job 加 `deliver: {file}`，收工事件掛點觸發 `deliver_job`：
Telegram 走 `sendDocument`（檔＋caption），Slack 走新版三步上傳（`getUploadURLExternal` → 傳 bytes →
`completeUploadExternal`，`files.upload` 已停用，需要 `files:write`）。目標格式沿用 cogito 的 `<平台>:<id>`，
連「id 長得像 token 就擋」那條理由一起沿用。誠實三條：檔不在或 mtime 早於開跑 → 改送收工訊息並明講「沒有產出」；
Slack HTTP 200 但 `ok:false` → 寫失敗不寫已送；老闆中止的班表任務不交付。三條都驗過紅。
token 放橋的 `.env`（與 cogito 同名、同一把），永不提交。

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

觸發統一用橋的班表；**執行引擎按例行事選**（job 的 `engine` 欄位；沒指定才落到外殼的選擇／人設）：

- 省錢、要免設定的抓網頁 → **CLI**
- 要審批、記憶蒸餾、Slack 推播、政策裁決 → **cogito**

第一步只做 ①＋一條 GitHub trend 範例班表，跑幾天看實際產出，再決定 ②③ 的深度。

## 四、一句話總結

排程器早就在，缺的是「每天」這個粒度與誠實的資料源；別為了數字員工去用 cogito 的內建 cron——那條辦公室看不見。

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

**第二、三張班表（同日）**：小樺 `intel-daily`（市場情報，判斷材料不下判斷，haiku）與小美 `pm-daily`（PM 觀點：對 Pixffice 的意涵、
做／不做／觀察、可機械檢查的驗收標準），都是每天 09:00、CLI、交付到 Telegram／Slack。來源沿 Aaron 自己的
`midnight-diner-market-intel` 的作法：RSS 清單、只取 24 小時內、抓不到就寫進「來源狀態」不補——英文 techcrunch／cnbc／
yahoo finance／marketwatch／nikkei asia（去掉早已停掉的 feeds.reuters.com），中文 technews／ithome／inside／bnext。
RSS 用 WebFetch 讀得出項目（haiku 探針：technews 與 techcrunch 各三則帶時間與連結，零拒絕），網域已放進 office profile 白名單。
新增 `schedule_file_valid` 合約測試：員工存在、引擎認得、deliver 帶 {date}、名字不重複——人名打錯班表會靜默略過，這種錯不該等到 09:00。

**班表上畫面（同日）**：先前班表完全不在 UI 上，老闆要知道誰幾點做什麼只能讀檔。現在檔案卡多一段「🗓 班表」
（何時、名稱、引擎、交付檔、上次跑），名冊上有班表的人掛 🗓。資料仍只有 `schedule.json` 與防重戳記，畫面不另存。
順手把「班表任務開跑」那行改走派工既有的寄放機制，掛在它開出的那張卡上，不再落在前一張卡的尾巴（卡 237 尾巴那行 239 的開跑就是這個 bug）。

**小樺、小美試跑（同日 16:01）**：兩人 90 秒內收工，報表都送到 Telegram 與 Slack。品質對得上人設：小樺每條帶來源、時間、
「信心：單一來源（未驗證）」；小美三條 PM 觀點各附對 Pixffice 的意涵、做／不做／觀察、可機械檢查的驗收標準。兩份報表開頭都有來源狀態表，
9 個 feed 4 個壞（MarketWatch 連不上、Nikkei 與 bnext 404、iThome 對 WebFetch 回 403）——**誠實地列出來，沒有補**。
抓到兩件要修的：
- 小美的報表寫進了 `office_p01/shop_coupon/`——沒綁 repo 的班表任務跑在 `agent_dir()`，那個優先拿上一張卡的 worktree。
  改成 `job_workdir`：沒綁 repo＝工作區根，交付找檔用同一個算法；驗紅過（不指定 cwd → None）。今天那份已手動搬回根。
- feed 網址用 curl 逐一探過：Nikkei 正確路徑是 `/rss/feed/nar`；MarketWatch 改走 `feeds.content.dowjones.io`；bnext 兩個路徑都 404、
  iThome 只擋 WebFetch 的 UA——兩個都換成 TechOrange。網域補進 profile 白名單。

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

## 二之後：可審計、可回溯、可人工介入（2026-09-08 起）

### 人工介入（CLI）—— ✅ 已完成

`-p` 沒有人能回答權限提問，先前一律拒絕、中途插不了話。Claude Code 有兩個為此準備的入口，都探過：
- **PermissionRequest hook**（`backend/tools/office_permission_hook.py`，橋啟動時同步進員工 profile 的 settings.json）：
  請求 POST 到橋的 `/office/permission`，橋用 cogito 審批卡的同一個樣板開卡（倒數、走到老闆房門口、外殼的放行／駁回鍵全部沿用），
  等老闆決定後回 allow／deny 給 hook。無人值守（班表任務）立刻拒、逾時拒、橋連不上拒——沒人可問時「等」不是安全，與 cogito 的 WithUnattended 同一條。
- **stream-json 輸入**：提示改從 stdin 送、stdin 保持開著，`/steer` 就是再送一則使用者訊息；實測第二則會排隊、各自一個 result，
  所以插話後多等一輪 result 才算收工。
驗紅三條：approve 不交回 hook（審批卡等到逾時）、無人值守不擋（變成等逾時）、插話不多等一輪（卡提早關）。

**活跑（2026-09-08 11:41）抓到一條關鍵事實**：`-p` 下 PermissionRequest hook **只有帶 `--permission-prompts none` 才會被問**——
沒帶時「無人可提問」直接拒，hook 連跑都沒跑（第一輪：WebFetch 立刻被拒，帳本只有 permission.denied）；帶了之後請求進辦公室，
審批卡跳出、外殼放行、WebFetch 真的跑、拿回「Example Domain」，帳本 approval.asked → approval.approved by=office-web。
旗標的語意不是「關掉提問」，是「提問改由 hook 回答」；合約測試釘住 argv。
另一條：橋跑著時把帳本改名歸檔，記憶體序號接著寫進新檔、鏈從頭就斷——現在每次寫之前先看磁碟檔尾，輪替就從 1 重起鏈；
測試整個行程改寫到暫存目錄，不再往真帳塞假的（踩過：真帳前 92 筆全是測試）。

### 回溯（兩個引擎）—— ✅ 已完成

卡片開卡時記下引擎、session、開始時間（CLI 的 start 事件帶 session id；cogito 沒帶就是一個頻道一條 `office_<aid>`）。
`/office/trace/<aid>/<卡號>` 把兩種完整紀錄解成同一種步驟清單 {at, kind, name, text, ok}：CLI 讀 profile 的
transcript jsonl，cogito 讀 `workspace/.sessions/office_<aid>-*.json` 的 history；老闆派的活共用固定 session，
所以用卡的時間切，班表任務每次新 session 整檔就是一次。卡頭多一顆 🧾，點開在卡裡列步驟，失敗的標紅、
cogito 動作前的思考列成 💭；CLI 的 thinking 只有簽章，不列也不假裝有。驗紅：拿掉時間切 → 昨天的紀錄混進來。

### 稽核（兩個引擎）—— ✅ 已完成

工作串是可覆寫的投影，要「每個裁決都留得下證據」得另外有一本只能往後寫的帳。`backend/audit/ledger.jsonl`：每筆帶
前一筆的 hash，自己的 hash 蓋住前一筆＋內容，改、刪、插任何一筆從那筆起全部對不上，`/office/audit` 回最近紀錄加整條鏈的驗證。
落帳點：任務開始／結束／中止、審批被問／放行／駁回／逾時、政策拒絕（cogito guard 的兩種拒絕只印 stdout，橋從工具錯誤事件的
字首認出來落帳；CLI 無人值守拒絕）、權限被擋、插話、交付成功／失敗。x402 那次 ledger 的形狀（Deny 也落帳）搬回來，
對象從支付改成所有裁決。檔案卡底下多一個「🧾 稽核帳本」面板。驗紅：驗證不比內容 hash → 竄改驗不出；政策拒絕不落帳。

**三件事現在的位置**：人工介入兩個引擎都有審批卡與插話（差別只剩 cogito 的審批來自它的 guard、CLI 的來自 Claude Code 的權限層）；
回溯兩邊都能從卡一鍵到完整紀錄（推理文字只有 cogito 有）；稽核一本帳共用。還沒做的：審批者身分（現在一律 office-web，單一老闆）、
帳本外送（放到辦公室以外的地方才算真的不可竄改）、cogito 端 guard 直接落帳（現在靠字首認）。

### 看板走 CLI —— ✅ 已完成（2026-09-08）

Claude Code 本來就有子 agent，看板跑不起來是辦公室三條線沒接：
1. **人設可點名**：`~/.claude/agents` 不吃 CLAUDE_CONFIG_DIR、project 層要每個工作區各放一份，所以改成 `--agents` JSON
   隨派工帶上（session-only、永遠跟人設同步）。`subagent_type` 只准小寫英文，persona yaml 多 `slug`（xiaomei／laoxu…）。
2. **主持人守則引擎中立**：點名表兩欄（cogito 名字／Claude Code 代號）、背景派工與等待各自的說法；Claude Code 沒有 await 工具，
   完成通知會自己來，守則明講不要輪詢。
3. **投影**：實測 Claude Code 的子 agent 事件形狀——`system/task_started`（tool_use_id、subagent_type、description）、
   帶 `parent_tool_use_id` 的 assistant／user（要 `--forward-subagent-text`）、`system/task_notification`（交件）、
   `background_tasks_changed`。橋把它們翻成 cogito 的詞彙 `spawn_subagent:<名>`、`[Subagent:<名>] …`、`subagent_await` 收件格式，
   起身入座、子卡、交付戲一行不改。背景子 agent 沒回來前 `result` 不算收工（`-p` 會等它們再跑一輪），驗紅過。
4. **審批**：子 agent 的權限請求走同一個 hook，cwd 一樣是看板工作區，對到看板；卡上暫時看不出是哪個子 agent 的（hook 輸入沒帶）。

活跑探針：`--agents` 定義 xiaomei／laoxu，主持人背景派兩人、各自回意見、完成通知、彙整——三個 result、一次 `-p`。

## 三、引擎怎麼選

觸發統一用橋的班表；**執行引擎按例行事選**（job 的 `engine` 欄位；沒指定才落到外殼的選擇／人設）：

- 省錢、要免設定的抓網頁 → **CLI**
- 要審批、記憶蒸餾、Slack 推播、政策裁決 → **cogito**

第一步只做 ①＋一條 GitHub trend 範例班表，跑幾天看實際產出，再決定 ②③ 的深度。

### 職缺站實測（2026-09-10，小安 jobs 班表）

第一份報告 0 筆、四個操作被擋——不是小安偷懶，是資料源：從這台機器與 Claude Code 的 WebFetch 各探一次，
- 104：搜尋頁／職缺頁全 403／402（Cloudflare 驗證），curl 換瀏覽器 UA 也一樣；
- Cake：curl 200 但 WebFetch 403（看 UA 擋）；1111：搜尋頁是 JS 殼，WebFetch 只看到「安全驗證失敗」；
- **Yourator**：搜尋頁是 JS 殼，但 `/api/v4/jobs?term[]=<關鍵字>&page=N` 是公開 JSON（每頁 20 筆、`hasMore` 翻頁，
  欄位 name／company.brand／location／salary／lastActiveAt／path），職缺內頁是伺服器端渲染，WebFetch 兩者都拿得到。
  `lastActiveAt` 只有「一天內／一週內更新」這種粒度，沒有刊登日——7 天內就用這欄。

決定（Aaron，2026-09-10）：只剩一站不值得每天跑，**jobs 班表取消**，小安只留 Threads 班表；找職缺留在人設裡當臨時派工用。
要重開得先有第二個來源：走瀏覽器（playwright）或各站 API 金鑰。

### Threads 主題輪替（2026-09-10）

小安第一份 Threads 報告問了三件事，Aaron 的答覆：查法改成**先巡追蹤帳號的個人頁再搜**（搜尋引擎對 Threads 索引落後約一個月，這是小安實測出來的）；
x402 放寬到 30 天；加一個主題「職缺招募／接案外包」；並要一個 **trend rotate**：主題連續幾天沒料就自動冷藏、把之前冷藏的主題撿回來查，名單靠命中的作者長大——資料飛輪。

做法：小安在自己的工作區維護 `topics.json`（主題：queries／window_days／miss_streak／hits_total／status；帳號：last_hit／miss_streak），
規則寫死在班表任務文裡：主題 miss_streak 到 3 冷藏並復活 parked 最早的 cold 主題；帳號 miss_streak 到 7 移到 cold_accounts；命中的新作者自動入名單。
報告多一節「主題輪替」把每次增減與理由寫出來，老闆看得到機制在轉。
**查法再修（同日，Aaron 看報告指出搜尋結果太差）**：回頭看 trace，7 次 WebSearch 全是「site:threads.net／.com ＋ 中英 OR 串 ＋ September 2026／本週」這種查詢。我自己用同一顆 WebSearch 實測：短中文查詢（「數字員工 AI agent」「x402 支付」「徵 AI agent 工程師」）配 `allowed_domains: ["threads.com"]`，每個都回 10 則對題貼文、含本週的；`threads.net` 對 Anthropic 爬蟲是擋的（帶進 allowed_domains 直接 400），她的 site:threads.net 查詢因此全回非 Threads 結果。所以「索引落後一個月」一半是查法造成的。任務文改成：只用 threads.com、一種語言一查、2～4 詞、不用 OR、不加日期字、每主題中文≥3 英文≥1，先用貼文代碼判新舊再逐篇核對。
ponytail: 輪替由 LLM 照規則改 JSON，沒有腳本強制；若實跑發現算錯 streak 或亂加帳號，再把規則搬進 `backend/tools/` 的小腳本讓她跑。

### 漏跑補跑（2026-09-10）

橋在 9:00 沒開著，那天的報告就沒有。補法：收件匣列出「今天到點卻沒跑」的班表（沒戳記、也沒今天那份 `deliver.file`），
每條一顆 ▶ 補跑、兩條以上多一顆「補跑全部」；檔案卡每條班表後面也有 ▶ 現在跑。走 `POST /office/schedule/run`（帶 name 或不帶）。
到點與補跑同一個入口 `fire_job`，守門只寫一次：**今天那份報表已經在了就不重跑**——補跑過再到點、到點過再手按，都只跑一次。
沒有 `deliver.file` 的班表看不出「有沒有產出」，只靠戳記防同一小時重複。

### 小安改專注職缺（2026-09-10 下午）

Aaron 看了 Threads 報告：內容凌亂、沒附連結、主題太散。決定：**小安只做職缺與外包需求收集**，來源順序 **MCP → 公開 API／WebFetch → WebSearch**；
Threads 班表從 schedule.json 下架（範例檔保留）。實測出來的資料源：

| 來源 | 結果 |
|---|---|
| **JobSpy MCP**（`mcp__jobspy__search_jobs`，Indeed 台灣＋LinkedIn） | 免金鑰，20 筆／秒，有日期有連結。裝法見 `backend/tools/office_mcp_setup.sh`（docker 替身、釘 sdk 1.10.2 與 zod 3.25，否則工具 schema 是空的、Claude Code 看不到） |
| LinkedIn 訪客搜尋 API（`/jobs-guest/jobs/api/seeMoreJobPostings/search`，`f_TPR=r604800`） | WebFetch 200，10 筆／頁，有相對時間 |
| Freelancer 公開 API（`/api/projects/0.1/projects/active/`） | 只有 `full_description=true` 且 **query 是單一個字** 才會過濾（"AI agent" 回全站 721 筆） |
| Yourator `/api/v4/jobs` | 第三順位（只有「一天內／一週內更新」粒度） |
| **104（MCP）** | Aaron 問「104 應該有 MCP」——有兩顆社群的：`mcp-server-104`（npx，cycletls）與 `job104-mcp`（uvx，curl_cffi），都靠**瀏覽器 TLS 指紋**過 Cloudflare、走非官方端點、免登入。實測 `mcp-server-104` 一次查回 22 筆含 appearDate 與連結。風險講明：104 ToS 與 IP 封鎖；用法限一天一輪、不翻頁。置頂廣告要 `excludeFeatured` 濾掉（'AI Agent' 前兩筆是主管秘書與視覺設計）。**104 只有 `appearDate`＝更新日，沒有刊登日**（search 與 detail 都只有這個欄位，官網也只顯示更新日期），第一輪 100 筆全是同一天。2026-09-11 起改由小安記 `seen.json` 首見帳：104 的窗口與「新出現／消失」都用首見日算，第一天全是首見、隔天起才準 |
| Cake／1111／Upwork | 機器人驗證，不試 |
| **JobFrog**（正確網址 www.job-frog.com，Aaron 補的） | Next.js：首頁與 /today 是 JS 殼，但 **/companies/<slug>（92 家）伺服器端列最新 30 筆含「N 天前」、/jobs/<id> 有「發布：日期」**；sitemap 3684 筆但 WebFetch 讀會截斷。robots：`*` 允許、禁 /api/ /go/，ClaudeBot（訓練爬蟲）禁、Content-Signal use=reference——只讀公司頁與職缺頁，不碰 API 與跳轉連結。列第二順位。2026-09-11 Aaron 要「關鍵字搜、不鎖公司」：站內搜尋是 JS 走被禁的 /api/，沒有伺服器端搜尋頁（/search、/?q=、/jobs?q= 都 404 或殼），只能 WebSearch 限 job-frog.com 再逐篇核日期——召回受搜尋引擎索引限制（'Web3 blockchain' 只回 1 則相關）。方向清單同時加 Web3／blockchain，104 與 JobSpy 關鍵字跟著加 |
| Tasker／PRO360 | 搜尋頁路徑 404，未接 |

報告格式改成固定五段、每列必須有連結與刊登日、表外每段最多兩行——「沒連結＝沒資料」寫進人設。

### 收件匣清除與帳本封存（2026-09-10）

老闆說有些訊息冗余、不想再看。兩層，刻意分開：
- **收件匣「🗑 清除」**：只藏「最近」到目前序號（`localStorage.inboxHide`），待處理永遠顯示；是老闆這個人的偏好，存瀏覽器，帳本一筆不少。
- **稽核面板「📦 封存」**：`POST /office/audit/archive`——這本改名 `ledger-<時間>.jsonl` 留在 `backend/audit/`，新本第一筆 `ledger.archived`
  記舊本檔名／筆數／最後 hash，兩本接得起來；面板與收件匣只讀新本所以清空。稽核帳從來不刪——「清除」是收進抽屜。
  空本不封存。測試 `audit_archive`，拔掉 last_hash 驗紅（KeyError）。

### 花費面板（2026-09-13，來自老徐押 OmniRoute）

老徐連兩天押 OmniRoute（352 家供應商的 OpenAI 相容 gateway），理由是「每家供應商各接一次、成本散在各處、換供應商要改程式」。
對照實況：cogito 本來就是單一 provider 抽象（Claude 或任何 OpenAI 相容端點，換供應商改兩個環境變數）、每 session 有 CostTracker 與
`MaxCostUSD` 熔斷、橋把真實花費攤在卡上。**真正缺的是彙總**：沒有每人／每日／每引擎的加總。
OmniRoute 不合適的三點：員工 CLI 走訂閱，經 gateway 就變 API 計費（它的「Subscription 層」是把訂閱 OAuth 拿到代理用，踩條款）；
省 token 靠壓縮提示，對要逐字原文的報告是品質風險且數字沒人驗；「免費 token 池」把請求散到 152 家免費供應商，稽核鏈斷在 gateway 外。

做的是小的：`GET /office/costs?days=N` 從稽核帳（含封存本）彙總 `task.done`，外殼 💰 面板每人每引擎一列。
CLI 的 result 現在帶 `usage`（token 進／出／快取）與 `api_equiv`（Claude Code 自報的 total_cost_usd，換算值）到卡與帳上，
**名字刻意不叫 cost**：訂閱不這樣扣，實花與換算分欄、不相加。cogito 單次上限讀 `workspace/.claw/config.json` 的 max_cost_usd，讀不到就不顯示。
OmniRoute 若要試，只把 `COGITO_REFLECT_MODEL` 指過去看一週。

### Codex（GPT-6）review 四條（2026-09-14）

全部核實成立，全部修，三條 Python 的各留一個回歸斷言並驗紅：
1. **WebGL 每 3 秒多開一條 WebSocket**：套件（endel/NativeWebSocket）的 WebGL `Connect()` 直接回 `Task.CompletedTask`，
   BrainGateway 以為它會停到斷線。改成另等一個在 `OnClose` 完成的 TaskCompletionSource，兩種平台都成立。**尚未起 Unity 實測**，要重建 WebGL 才會生效。
2. 中文檔名預覽 500：檔名自己塞 header 會 `UnicodeEncodeError`，改交 `FileResponse(filename=…, content_disposition_type="inline")` 做 RFC 5987。
3. 存檔失敗不重試：`save_state()` 先清 `_dirty` 再寫，改成替換檔案成功後才清。
4. 失效符號連結讓整個目錄列不出來：`listing()` 逐筆 `stat()` 包 `OSError` 跳過。
維護性那條（main.py 4,200 行要拆）同意方向，先不動——拆檔是純搬家、沒有行為變更可驗，等下次要大改哪一塊再順手把那塊搬出去。

### 中止後的班表任務要能重跑（2026-09-14）

Aaron 回報：早上 09:30 補跑四條，中途按中止兩條（老徐、小安），之後「無法再要求 agent 重新執行」。核對帳本：中止後沒有任何 task.start。
手動 `POST /office/schedule/run` 實測是通的（老徐當場開跑），所以不是跑不了，是**找不到地方跑**：中止那一刻戳記已經蓋了這小時，
`missed_jobs` 不把它當漏跑、收件匣就沒有「補跑」列，只剩檔案卡上那顆小小的 ▶。
第一版記了一份 `sched_stopped` 狀態，Aaron 再指出老徐也該重跑——他 21:08 重跑到一半被 21:13 的橋重啟砍掉，收的是 task.done(error) 不是 task.stopped，狀態沒記到。
所以改成**只看帳本、不另記狀態**：戳記今天蓋過、沒有產出、而且這個人今天最後一次任務收法不是 ok（被中止＝stopped、被砍／出錯＝error）→ 列進收件匣，文字分「今天被中止」與「今天沒跑完（中斷或出錯）」；正常收工或正在跑就不列。順手修一句謊話：橋關閉時砍掉的 CLI 任務先前寫成「老闆中止了這個任務」，現在只有 `stopped` 集合裡真的按過中止的才這樣寫。測試 schedule_manual_run 涵蓋三種收法，拔掉判定驗紅。
外殼那顆「▶ 補跑」按了沒反應（實際回報）：班表名稱用 `JSON.stringify` 塞進 `onclick="…"`，JSON 的雙引號把屬性截斷、整顆鈕變語法錯誤。改成 DOM 事件綁定，值不經過 HTML；全份內嵌腳本過 `node --check`。教訓：帶使用者資料的 handler 一律綁事件，不拼進屬性字串。

### 小安有了身體（2026-09-14）

Aaron 要總機小安像其他人一樣有行為與動作，但活動範圍在座位附近、大部分時間在座位辦公。
看了櫃檯叢集 obj_03 的美術：她站的那格被書架（北）、檯面（西南）、螢幕桌（東）四面圍住，碰撞圖也是家具格，
**沒有走得出去的口**——要走就得穿過櫃檯，或重畫櫃檯開一個口。決定：她**不走位**，其他全部照員工來。
- Unity：`BuildReception` 改走 `BuildOne(physical: false)`——Kinematic Rigidbody、不掛碰撞／FakeBrain／NPCSeparation，
  其餘（NPCSprite 全套姿勢、徽章、泡泡、對話框、NPCAgent）與員工相同；`tools/make_character.py 10` 補齊 hurt／pick_up／lift／throw 幀。
- 橋：人設 `post: fixed` → `stays_put()`；`goto()` 對她不送 move_to、改擺回工作姿勢（`SIT_AT["p10"]="face_down"`）；
  `goto_then_pose()` 當場擺（等審批＝在櫃檯掏手機）；生活迴圈不抽 waypoint，3:1 在辦公／接電話；久沒事就在櫃檯趴睡；
  出錯 `hurt_down`、遞交 `gift_down`。
- 順手修：`start_agents` 先前對名冊上每個人都開生活迴圈，畫面上沒有的小安每輪 move_to 都等 30 秒逾時、
  還把 `canvas_stale` 打成 True（之前看到的 stale 旗標無故閃就是它）。現在只給 Unity 回報的人開迴圈。
- 測試 `stay_put`，拔掉守門驗紅。若哪天要她真的走出來：改 West 碰撞圖開一格＋重畫櫃檯那格的口，橋端把 `post: fixed` 拿掉就行。

### 預設 Opus、子 agent 依難度選模型（2026-09-15）

Aaron 定：每個員工預設跑 `claude-opus-5[1m]`；只有派子 agent 時，才由主 agent 依那件事的難度選模型。不做開關。
- **主 agent**：`Agent.model` 人設沒寫就用 `OFFICE_DEFAULT_MODEL`（預設 `claude-opus-5[1m]`）；拿掉小樺、小安（Haiku）與老徐的模型釘；
  清掉外殼保存的選擇（小美、阿海、小樺被釘在 Haiku）。送 cogito 時拿掉 `[1m]`（API 沒這個 id，claude-opus-5 本身就是 1M）。
  取捨：先前「人設沒寫就不送 model、別蓋掉聊天端 `model` 指令選的」這條原則被這個決定取代——現在每次派工都會送預設。
- **CLI 子 agent**：實測（haiku 主 agent 派同一個定義成 Sonnet 的子 agent）呼叫時帶 `model: haiku` 就跑 Haiku、不帶就照定義——
  per-call 參數蓋過定義。所以 `--agents` 的人設定義不再寫模型（預設繼承主 agent 的 Opus），判斷表寫進 `personas/office.md`。
  子 agent 第一則訊息帶實際模型：子卡掛一行「🧠 模型：…」、稽核帳落 `subagent.model`。
- **cogito 子 agent**：`spawn_subagent` 先前沒有 model 參數（只吃具名 agent 檔寫死的值）。cogito `38b1dd3` 補上 `model`（只收 haiku／sonnet／opus），
  蓋過定義；引擎端解成 Claude id，主引擎不是 Claude 就忽略沿用。cogito 的子卡目前**不顯示**子 agent 模型（只在 cogito log）。

### Codex 成為第三個引擎（2026-09-17）

Aaron 有了 Codex Pro 與 API key。先接 **Codex Pro（ChatGPT 訂閱）**，做法照 Claude Code 那條：`codex exec --json` 非互動。實測 codex-cli 0.154.0：
- 事件：`thread.started{thread_id}`、`item.started/item.completed{item.type}`（見過 agent_message、command_execution、error＝警告）、
  `turn.completed{usage}`、`error`、`turn.failed{error.message}`。事件**不報模型**，模型只在 rollout（`$CODEX_HOME/sessions/…/rollout-*-<thread>.jsonl`）的 turn_context。
- 人設：Codex 讀工作區 `AGENTS.md`，橋本來就同步了（實測「小測報到」）。
- 接續：`codex exec --json --skip-git-repo-check … resume <thread> -`（旗標要放在 resume 前面，否則「不在信任目錄」）。
  **resume 不帶 -m 會被換成 Codex 目前的預設**（gpt-5.6-sol → gpt-6-astra 並警告），所以帶回那條 thread 記錄的模型。
- exec 的 approval_policy 是 never、workspace-write 沙箱預設沒有網路；沙箱外操作直接失敗讓模型改道。
- 橋：`ENGINE_CODEX`、`run_codex_task`（事件→office 事件、用量與實際模型、老闆派的活接同一條 thread、班表開新的、/stop 砍行程）、
  `trace_codex`（讀 rollout）、外殼引擎選單多 Codex；子行程濾掉 OPENAI_API_KEY／CODEX_API_KEY（同 Claude Code 的計費坑）。
- 明講不支援：插話（exec 沒有第二則訊息入口）、審批（never）、看板（主持人要派子 agent，Codex 的 multi_agent 事件還沒接）。
- 模型選單（同日補，Aaron 回報選了 Codex 沒有模型可選）：清單讀 Codex 自己的 `$CODEX_HOME/models_cache.json`（它依帳號方案過濾過；只列 visibility=list、照 priority 排，員工的 home 還沒跑過就退回 ~/.codex 那份）。選擇記在 `codex_model_sent`，**跟 Claude 的 model_sent 分開**；優先序：這次選的 > 記住的 > 接續那條 thread 的模型 > OFFICE_CODEX_MODEL > Codex 預設；Claude 型號一律不送；「還原」收回。外殼兩份選單輪流重建會洗掉正在選的值，所以引擎是 Codex 時 Claude 那份不畫。
- 還沒做：API key 那半（cogito 目前一個行程只有一個 provider，要讓某些員工走 OpenAI 得改 cogito）、Codex hooks 接審批、Codex 子 agent 投影。
測試 `codex_engine`（假 codex 照實測事件形狀），拔掉 API key 過濾、resume 模型沿用各自驗紅。

## 四、一句話總結

排程器早就在，缺的是「每天」這個粒度與誠實的資料源；別為了數字員工去用 cogito 的內建 cron——那條辦公室看不見。

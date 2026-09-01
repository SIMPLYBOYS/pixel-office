# 對照筆記：munder-difflin（同一個問題的另一種解法）

> 來源：<https://github.com/chaitanyagiri/munder-difflin> 的 README / HIVE.md / SPEC.md，
> 讀於 2026-08-27。
>
> ⚠ **這份筆記沒有讀過他們的原始碼**——clone 在這台機器上被網路擋掉，只讀得到那三份
> 設計文件。所以下面凡是講「他們怎麼做」的，都是【規格上怎麼寫】，不是【程式碼怎麼跑】。
> 要照抄任何一條之前，先去把對應的實作看過。

## 它是什麼

把終端機的 agent CLI（Claude Code、Codex、Gemini…十二種）包成一個 Electron 桌面應用，
畫成一層像素辦公室：agent 是走來走去的角色、彼此傳訊是信封在桌與桌之間飛。
一個叫 Michael 的「GOD agent」坐 CEO 室當調度，只把關鍵決策升級給人。

**跟我們是同一個問題的兩種解法**，而且兩邊獨立長出了好幾條一樣的結論——這件事本身
就有價值：那些結論多半不是品味，是這個問題的形狀決定的。

|  | Pixffice（我們） | munder-difflin |
|---|---|---|
| 大腦 | cogito-agent（自己寫的 Go ReAct harness） | 別人的 CLI，十二種都收 |
| 畫面 | Unity WebGL + Web 外殼 | Electron + Pixi.js + xterm.js |
| 事件來源 | cogito 的 OfficeReporter → `/office/event` | Claude Code hooks → Unix socket |
| 協作 | 看板主持人 spawn 子 agent（星狀） | agent 互相寄信（點對點）+ GOD 調度 |
| 記憶 | markdown 一條一檔 + 提案閘 + 自動放行判準 | markdown 自管 + SQLite FTS，無審核閘 |
| 人在哪裡 | 結構化審批卡（外殼自己畫） | 直接用 CLI 原生的 tool-permission prompt |

## 一、兩邊各自長出來的同一個答案（不用改，但值得知道）

這幾條我們早就有，他們也有，而且理由講得幾乎一樣。互相印證＝可以更有信心地守住。

- **投影誠實**。他們的 SPEC 寫「every animation should actually tell you something you
  didn't know」，我們寫「假的成功比空白更糟」。同一句話。
- **單一寫入者**。他們是「no agent ever touches git（single-committer）」；我們是
  `personas/kanban.md` 的「只有你能改 board.json，子 agent 一律不准碰」。都是為了避免
  多寫入者互相蓋掉，而且都選擇「用架構擋」而不是「用鎖擋」。
- **拒絕向量檢索**。他們說 vector layer 跟 CLI-based runtime 不對盤；我們的衝突偵測
  用詞彙重疊分數，也沒上 embedding。兩邊都覺得那層太重。
- **失聯要有中間態**。他們的 pane 掉了先進 ghost 30 秒才歸檔；我們的 watchdog 判失聯
  前也有寬限。理由相同：突然消失會讓人誤判狀態。
- **markdown 是記憶的主格式**，索引是加速器不是真相。

## 二、值得學的（按「現在就能做 × 價值」排序）

### ① 記憶庫進 git、單一提交者 —— 直接解掉我們卡住的 `rev_rollback`

**他們**：整個 hive（agent 的檔案與記憶）就是一個 git repo，而且**只有 harness 提交**，
agent 一律不碰 git（理由是避免 `.git/index.lock` 壞掉）。

**我們**：memory-review 那場會議的 `rev_rollback` 卡寫的是「記憶庫進 git、一提案一
commit、放行錯的 revert 那條 commit、爆炸半徑鎖在單條」——**我們把它標成 blocked**，
理由是 `workspace/` 整個被 gitignore。

**但那不是真的做不到**：他們的做法正是答案——`workspace/.claw/memory/`（或整個頻道
工作區）自己是一個**獨立的 git repo**，跟外層那個 repo 無關，所以外層 ignore 它完全
不衝突。提交者只有橋（或 cogito），agent 永遠不碰。

- 我們已經有的一半：自動放行的 72 小時撤回窗（`memory_autopass.go`），撤回＝歸檔到
  `memory-archive/`。
- git 補的是另一半：**過窗之後**的回滾、以及**人工放行**那些的回滾。現在過窗即定案，
  錯了只能手動刪檔。
- 代價很小：一個 `git init` 加一次 commit 呼叫。風險是 repo 會長大，但記憶一條一個
  小檔，一年也不會有多大。

**這條我認為最值得做，而且是唯一「解掉一張已知卡住的卡」的。**

### ② 熔斷要有三階：steer → constrain → stop

**他們**：對「打轉、狂噴錯、燒超預算」的 agent 有一道 steer → constrain → stop 的階梯。

**我們只有 stop**。外殼上工作中只有「中止」一顆鈕，而且 `office_dispatch` 明文擋掉
工作中的新訊息（`{name} 正在工作中，收工後再派新任務`）。也就是說：**看到 agent 走偏
的當下，你唯一能做的是把它殺掉重來**——前面燒的錢全部作廢。

缺的是中間那階：

- **steer**：不中止，只塞一句話進去（「別再找那個檔了，路徑是 X」）。技術上就是往
  cogito 送一則訊息而不是新任務——`/office/dispatch` 現在的 verb 白名單
  （`approve`/`reject`/`/stop`）再加一個就是了。
- **constrain**：限制它接下來能用的工具或剩餘預算。這個要 cogito 端配合，比較大。

steer 這一階成本很低、價值很高：它把「發現走偏」到「能介入」之間的唯一選項從
「殺掉」變成「糾正」。

### ③ 訊息語意要有終止性（如果我們哪天做點對點）

**他們**：FIPA-lite 的七個 speech act，而且明文寫死
「only `request`/`query`/`propose` obligate a reply（純 `inform`/`done` 是終點）」，
再加一個 hop counter，超過上限就升級給 GOD 而不是讓兩個 agent 無限來回。

**我們現在不需要**——看板是星狀拓撲（主持人 ↔ 子 agent），子 agent 之間不對話，
所以 livelock 的風險結構上就不存在。

**但這條要記著**：哪天想讓小美直接問阿哲（不經過主持人），第一件要做的事不是
「開一條通道」，是先把「哪幾種訊息有回覆義務」寫死。他們用一句話擋掉整類 bug。

### ④ 成本從 transcript 讀真的，不要估

**他們**：解析 `~/.claude/projects/` 的 JSONL 拿真實花費，而不是自己估 token。

**我們**：`meeting-cost-visibility.md` 那場會討論過成本可視化，但外殼上目前看不到花費。
cogito 自己有成本熔斷（相機的 `CAM_DECISION` 就把它列為要推鏡頭的事件）。

若要做，照他們的原則：**顯示真實數字，不要顯示估計值**。這跟我們的投影誠實是同一條——
一個估出來的成本數字，跟一個假的進度條是同一種謊。

### ⑤ 子 agent 用 per-agent git worktree 隔離

**他們**：每個 agent 可以有自己的 worktree。

**我們**：~~全部共用同一個頻道工作目錄~~ **這段寫錯了**（2026-09-01 盤點修正）：
cogito 早就有——`internal/tools/worktree.go` 開 detached worktree、merge-back 用鎖序列化，
具名 agent 檔宣告 `isolation: worktree` 即用（`implementer.md` 就有宣告），非 git repo
時靜默降級為共享。**這條已完成，不是 action item。**

## 三、明確不學的

- **十二種 CLI 供應商 / BYOK**。我們的大腦是自己寫的 cogito，投影合約
  （`/office/event` 的 kind 分類）是為它設計的。收別人的 CLI 等於把合約降級成
  「解析終端機輸出」，那正是 SPEC 自己說的「fragile output parsing」。
- **用 CLI 原生的 permission prompt 當 HITL**。他們說「There is no separate approval
  queue」是優點——對桌面應用是。但我們的入口是 Web 外殼，人不在終端機前面，
  所以結構化審批卡（工具 chip、指令折行、倒數、核准／駁回）是對的方向，不要退回去。
- **雙資料平面裡的「終端機平面」**（tmux pipe-pane + xterm.js 顯示原始輸出）。
  我們沒有自己的終端機可以接；工作串已經是我們的結構化平面，而原始輸出對
  「老闆看辦公室」這個使用情境沒有價值。

## 四、一句話總結

他們把**廣度**做到極致（十二種 CLI、點對點信箱、GOD 調度），我們把**深度**做到極致
（投影誠實有合約測試會紅、記憶有四判準的自動放行閘、審批卡結構化、相機有優先級階梯）。

真正該搬過來的只有兩條：**記憶庫進 git 解掉 rev_rollback**、以及**熔斷補上 steer 這一階**。
其餘的價值在於印證——好幾條我們以為是品味的決定，別人獨立走到了同一個地方。

## 五、盤點現況（2026-09-01，對著 cogito 程式碼查的）

| # | 條目 | 狀態 | 依據 |
|---|---|---|---|
| ① | 記憶庫進 git | ✅ 已完成（cogito `533953d`，2026-09-01）。盤點時發現 `workspace/.git` 早已存在（agent 自己會 commit skills 進去），缺的只是沒人 commit 記憶——補上 `commitMemory`：單一提交者、一提案一 commit、撤回留帳、非 git 工作區靜默降級；既有 229 個記錄檔收成基線 commit | `evolve/memory_git.go` |
| ② | steer 這一階 | ❌ 未做。cogito 忙碌時明拒（`core.go` ⏳ 上一個任務仍在進行…可用 /stop）；橋的 verb 白名單只有 approve/reject//stop；`reminder.go` 的 nudge 是內部系統提醒，使用者塞不進去 | `chatbot/core.go` tryAcquire 分支 |
| ③ | 回覆義務＋hop cap | ➖ 不適用，維持不動。仍是星狀拓撲，沒有點對點信箱 | grep 無 mailbox/inbox |
| ④ | 成本讀真的 | 🟡 cogito 端**全有**（`session.TotalCostUSD` 真實累計、收工報「本次花費 $x」、`MaxCostUSD` 熔斷且 `path.go` 護著不讓 agent 自改）。缺的只剩投影：`officeEvent` 沒有 cost 欄位 → 外殼看不到。done 事件帶上＋外殼顯示即可 | `office_reporter.go` 欄位表 |
| ⑤ | worktree 隔離 | ✅ 已完成（筆記原文寫錯，已更正見上） | `tools/worktree.go`、`agents/implementer.md` |

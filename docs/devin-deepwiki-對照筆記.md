# 對照筆記：Devin Cloud 的 repo 中心 UX 與 DeepWiki（實測驗真版）

> 來源：Aaron 對 Devin Cloud 的使用觀察（2026-09-01）＋ 對 DeepWiki 生成內容的**實際深讀與驗真**——
> 拿 `fugaku-sanjūrokkei-pixel`（Aaron 自己的公開 repo）在 deepwiki.com 索引出完整 wiki
> （27 頁、7 章，索引 commit `0d9d5e` ＝ 本地 HEAD，無版本落差），抽讀三頁，
> 再逐條對本機原始碼驗引用。**這份筆記的判斷建立在驗過的證據上，不是產品頁面的轉述。**

## 根本差異：repo 中心 vs 員工中心

Devin 的世界裡 **repo 是恆久物**、agent 是隨叫隨到的工人——所以授權、掃描、wiki
全部掛在 repo 上。Pixffice 相反：**員工是恆久物**（常駐辦公室、有記憶、有人設、有職責），
工作流經人。這不是誰對誰錯，是產品靈魂——把 Pixffice 改成 repo 中心等於把辦公室拆了。

所以值得學的不是「以 repo 為核心」，而是「**repo 作為任務的一等參數**」。
兩種敘事的差別：Devin 是「把 repo 交給雲」，Pixffice 是「把案子交給某個人」。

## 一、三條可落地的（依「解真實缺口 × 成本」排序）

### ① 任務綁定真實 repo —— 目前最真實的缺口

現在辦公室派工，員工一律在自己的頻道工作區（`workspace/channels/<id>`）幹活——
那是沙箱，不是你的專案。要讓員工真的改某個 repo 的程式，路徑是彆扭的。

而 cogito **零件都有**：`worktree.go`（detached worktree＋序列化 merge-back）、bash 有
`gh`。缺的只是派工語意：「這個任務在 repo X 上做」——composer 加個 repo 欄位、
橋把 workdir 指過去（或 clone 進頻道工作區）。做完這條，Devin 的「選 repo → 派工」
就等價存在，而且仍是員工中心的敘事。

### ③ Security scan per repo —— 幾乎免費

security-auditor 人設（SUB_NPC 映射 p07）＋ cogito 現成的 cron（`.claw/cron.json`）
＝「保全每週巡一次某 repo、報告落工作區」。不用新機制，只是一個排程任務的措辭。

### ② DeepWiki 的對應物 —— 「員工養的 repo 文件」（本筆記的主角，見下）

### 明確不學的

GitHub OAuth 式的「平台代管你的 repo」。Pixffice 自託管、本機優先——員工直接用
磁碟上的 repo 與使用者自己的 `gh` 身分，不需要中間再插一層授權代理。

## 二、DeepWiki 實測：好的那半（骨架值得抄）

對一個單檔像素遊戲 repo，它切出了幾乎是資深工程師手筆的目錄：

```
1 概覽（含 Getting Started、玩法迴圈）
2 應用架構（狀態機 / 地圖引擎 / 場景引擎 / 平移縮放 / Web Audio / 證書 / i18n 各一頁）
3 資料層（prints.json schema、命中座標與 QA 流程）
4 資產管線（五支 Python 腳本各一頁）
5 資產參考（三層圖檔、PWA）
6 內容與授權（圖版目錄、素材來源）
7 詞彙表
```

每頁固定結構，四個裝置都值得抄：

1. **散文開場**講這個子系統是什麼、為誰存在；
2. **「概念 → 程式實體」對映表**——如「Collision Check → `checkHit()` → 疊代 p.hits」。
   Overview 還有一張「Technical Map: From Concepts to Code」，把「Browse Map / Play
   Level」這種自然語言概念直接映到 `#world`／`openScene()`。這是「人跟 agent 共用
   心智模型」的關鍵形狀：人記概念、agent 記符號，表是兩者的橋。
3. **mermaid 流程圖**（真渲染；抽讀頁面均 ~30 節點）；
4. **每節尾 Sources 引用**（file:line）。

內容層抽驗全對：zero-build 哲學、三層資產（320/1000/2600px）共用歸一化座標、
yō-scale 五聲音階 BGM、QA 腳本用途——**函式名、檔名、schema 結構全部真實存在**
（`relaxNodes`／`openScene`／`qa-hits.py`／`hits[].rect` 逐一 grep 過）。

## 三、DeepWiki 實測：壞的那半（引用是裝飾）

**file:line 引用是漂的**，而且不是版本落差（索引 commit＝本地 HEAD）：

| wiki 宣稱 | 實際（本機 grep/sed） |
|---|---|
| `openScene` 在 `index.html` 438-460 | 在 **498**；438 行是富士山的 SVG |
| `checkHit` 在 `index.html` 705-720 | 705 行是 BGM 的音符資料 |
| `openScene(n)` 按索引取 PRINTS | 簽名是 `openScene(p)` |
| `prints.json` 37-45 是 hits/rect | ✅ 準（結構規律的 JSON 就對得上） |

模式很清楚：**行號是模型猜的，不是解析的**。規律結構的小檔猜得中，一千多行的
單檔 HTML 就漂。引用長得可信、大多驗不過——正是「假的成功比空白更糟」的教科書案例。

## 四、對「員工養 repo 文件」的設計結論

1. **抄骨架**：章節樹、每頁四裝置（散文＋對映表＋mermaid＋Sources）。
2. **不抄引用的做法**：這是我們的主場優勢——DeepWiki 在雲端猜行號，我們的員工
   **就在 repo 旁邊**：行號 grep 出來再寫、寫完可以驗（斷言「引用的行真的含那個
   符號」，驗不過就降級成只給檔名）。cogito 紀律第 9 條「每個說法追得回來源」直接適用。
3. **活文件，不是快照**：DeepWiki 是索引時一次生成；我們天然是「任務收工後增量
   更新」——哪位員工負責哪個 repo 的文件是長期職責（cogito 的 goal + cron 是現成
   機制），文件有人養，比一次生成值錢。
4. 第一刀要決的四件事：哪位員工負責、何時更新（收工 hook？cron？）、產物放哪
   （repo 內 `docs/wiki/`？工作區？）、引用怎麼驗（grep 斷言）。

## 五、一句話總結

DeepWiki 證明了「概念→程式碼的對映表＋固定頁面結構」是對的形狀，也證明了
雲端生成的引用不可信。抄它的骨架、用我們的誠實引擎——員工就在 repo 旁邊，
沒有理由猜。

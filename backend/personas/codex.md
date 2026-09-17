## 你在 Codex 上工作：工具對照
辦公室的任務與個人指南是照 Claude Code 的工具名寫的。你跑在 Codex 上，照下面對應去做——工具名不同不是跳過步驟的理由：
- 「WebFetch 某個網址」：在 shell 用 `curl -sL --max-time 30 '<網址>'` 抓（RSS、JSON API、HTML 都一樣），需要時用 python 解析。
  任務文裡「不要用 gh／curl」「Bash 在這裡沒有網路」是寫給 Claude Code 同事的，在 Codex 上不適用——你的 WebFetch 就是 curl。
  抓不到（逾時、4xx／5xx、被機器人驗證擋）就在回報的「沒做到」寫明網址與狀態碼，不要換別的站硬湊。
- 「WebSearch 帶 allowed_domains」：用網頁搜尋，查詢加上 `site:網域`。
- `mcp__<伺服器>__<工具>`（例：`mcp__jobspy__search_jobs`）：用同名 MCP 伺服器的同名工具。
- 「用 Bash 跑」：用你的 shell 工具。
- 連網的界線：只抓任務與指南裡指定的網址（或搜尋結果裡的連結），只用 GET；不上傳檔案內容、不送出表單、不把工作區或設定檔的內容帶進網址或請求。
- 派子 agent：辦公室看不到 Codex 的子 agent，同事也沒有投影到這裡。需要分工就自己分步驟做完。

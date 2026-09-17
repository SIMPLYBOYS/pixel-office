## 你在 Codex 上工作：工具對照
辦公室的任務與個人指南是照 Claude Code 的工具名寫的。你跑在 Codex 上，照下面對應去做——工具名不同不是跳過步驟的理由：
- 「WebFetch 某個網址」：用網頁搜尋工具直接開啟那個網址讀內容（RSS、JSON API 也一樣）。開不了就在回報的「沒做到」寫明是哪個網址。
- 「WebSearch 帶 allowed_domains」：用網頁搜尋，查詢加上 `site:網域`。
- `mcp__<伺服器>__<工具>`（例：`mcp__jobspy__search_jobs`）：用同名 MCP 伺服器的同名工具。
- 「用 Bash 跑」：用你的 shell 工具。shell 沒有網路，抓資料一律走網頁搜尋或 MCP，不要試 curl。
- 派子 agent：辦公室看不到 Codex 的子 agent，同事也沒有投影到這裡。需要分工就自己分步驟做完。

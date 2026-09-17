#!/bin/sh
# 員工 profile（~/.claude-office）的 MCP：JobSpy（Indeed／LinkedIn 等職缺，免金鑰）。
# 2026-09-10 實測踩到的三件事都在這裡處理：① 上游伺服器固定用 docker 跑 python，這台沒有 docker → DOCKER_CMD 指向替身腳本；
# ② 上游 package.json 的 sdk ^1.10 會裝到新版、prompt 註冊直接炸 → 釘 1.10.2；③ sdk 底下 zod 被解成 v4、工具 schema 變空、Claude Code 因此看不到工具 → 釘 zod 3.25。
# 跑完要在 ~/.claude-office/settings.json 的 allow 加 "mcp__jobspy"（無人值守：沒放行＝拒）。
set -e
OFFICE="${CLAUDE_CONFIG_DIR:-$HOME/.claude-office}"
M="$OFFICE/mcp"
mkdir -p "$M"
# 釘住審過的 commit，並套上辦公室的安全修補（2026-09-17 安全稽核 Critical #1）：上游把 LLM 給的參數拼進 shell 字串 execSync，
# 提示注入就能在沙箱外執行任意指令。修補改成 execFileSync＋參數陣列，字串參數用 --flag=值（值以 - 開頭也不會被當成選項）。
# 上游修掉之前每次安裝都要套；已套過會自動略過。
JOBSPY_COMMIT=5f93ffe
PATCH="$(cd "$(dirname "$0")" && pwd)/jobspy-mcp-argv.patch"
if [ ! -d "$M/jobspy-mcp-server" ]; then
  git clone -q https://github.com/borgius/jobspy-mcp-server.git "$M/jobspy-mcp-server"
  git -C "$M/jobspy-mcp-server" checkout -q "$JOBSPY_COMMIT"
fi
git -C "$M/jobspy-mcp-server" apply --reverse --check "$PATCH" 2>/dev/null || git -C "$M/jobspy-mcp-server" apply "$PATCH"
grep -q "execFileSync(dockerCmd" "$M/jobspy-mcp-server/src/tools/search-jobs.js" || { echo "✗ jobspy 安全修補沒套上，停止安裝"; exit 1; }
[ -d "$M/venv" ] || python3 -m venv "$M/venv"
"$M/venv/bin/pip" install -q python-jobspy
cd "$M/jobspy-mcp-server" && npm install --silent && npm install --silent @modelcontextprotocol/sdk@1.10.2 zod@3.25.76 && npm dedupe --silent
cat > "$M/jobspy-run.sh" <<SH
#!/bin/sh
# jobspy-mcp-server 以 \`docker run --rm jobspy <args>\` 呼叫；沒有 docker，丟掉前三個字、用 venv 跑 main.py
shift 3
exec "$M/venv/bin/python" "$M/jobspy-mcp-server/jobspy/main.py" "\$@"
SH
chmod +x "$M/jobspy-run.sh"
python3 - "$OFFICE/.claude.json" "$M" <<'PY'
import json, sys, os
p, m = sys.argv[1], sys.argv[2]
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault("mcpServers", {})["jobspy"] = {"type": "stdio", "command": "node", "args": [f"{m}/jobspy-mcp-server/src/index.js"],
                                            "env": {"ENABLE_SSE": "0", "DOCKER_CMD": f"{m}/jobspy-run.sh"}}
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print("mcpServers.jobspy 寫進", p)
PY
# 104：社群的 mcp-server-104（npx，釘版本）。它靠瀏覽器 TLS 指紋（cycletls）過 104 的 Cloudflare 驗證、走非官方端點——
# Aaron 2026-09-10 知情後決定用；只在班表那一輪低頻查，不翻頁、不重複打。allow 要加 "mcp__job104"。
V104="${MCP104_VERSION:-0.2.0}"   # 釘在審過的版本（先前取 npm 最新版＝每次安裝都可能換成沒看過的程式碼）
python3 - "$OFFICE/.claude.json" "$V104" <<'PY'
import json, sys
p, v = sys.argv[1], sys.argv[2]
d = json.load(open(p))
d.setdefault("mcpServers", {})["job104"] = {"type": "stdio", "command": "npx", "args": ["-y", f"mcp-server-104@{v}"]}
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print("mcpServers.job104 寫進", p, "版本", v)
PY
# 自檢：工具 schema 不能是空的（空＝Claude Code 會把工具丟掉）
(printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"setup","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'; sleep 2) \
  | ENABLE_SSE=0 DOCKER_CMD="$M/jobspy-run.sh" node "$M/jobspy-mcp-server/src/index.js" 2>/dev/null \
  | grep -q '"searchTerm"' && echo "✓ search_jobs schema 正常" || { echo "✗ search_jobs schema 是空的，Claude Code 看不到這顆工具"; exit 1; }

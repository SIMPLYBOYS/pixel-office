#!/bin/sh
# 員工 profile（~/.claude-office）的 MCP：JobSpy（Indeed／LinkedIn 等職缺，免金鑰）。
# 2026-09-10 實測踩到的三件事都在這裡處理：① 上游伺服器固定用 docker 跑 python，這台沒有 docker → DOCKER_CMD 指向替身腳本；
# ② 上游 package.json 的 sdk ^1.10 會裝到新版、prompt 註冊直接炸 → 釘 1.10.2；③ sdk 底下 zod 被解成 v4、工具 schema 變空、Claude Code 因此看不到工具 → 釘 zod 3.25。
# 跑完要在 ~/.claude-office/settings.json 的 allow 加 "mcp__jobspy"（無人值守：沒放行＝拒）。
set -e
OFFICE="${CLAUDE_CONFIG_DIR:-$HOME/.claude-office}"
M="$OFFICE/mcp"
mkdir -p "$M"
[ -d "$M/jobspy-mcp-server" ] || git clone -q https://github.com/borgius/jobspy-mcp-server.git "$M/jobspy-mcp-server"
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
# 自檢：工具 schema 不能是空的（空＝Claude Code 會把工具丟掉）
(printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"setup","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'; sleep 2) \
  | ENABLE_SSE=0 DOCKER_CMD="$M/jobspy-run.sh" node "$M/jobspy-mcp-server/src/index.js" 2>/dev/null \
  | grep -q '"searchTerm"' && echo "✓ search_jobs schema 正常" || { echo "✗ search_jobs schema 是空的，Claude Code 看不到這顆工具"; exit 1; }

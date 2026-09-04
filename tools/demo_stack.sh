#!/usr/bin/env bash
# demo_stack.sh — 一鍵拉起「請購單 for Agents」demo 的三個行程，Ctrl-C 一起收。
#
#   x402mock（本地 402 資源伺服器＋facilitator） → :4021
#   cogito   （只開 office HTTP 入口，不連 Slack/Telegram）→ COGITO_HTTP_ADDR（.env）
#   橋       （FastAPI，辦公室＋外殼）                → :8123
#
# 用法：
#   tools/demo_stack.sh                 # 價格 $0.05（小額，policy 自動放行）
#   PRICE=1.00 tools/demo_stack.sh      # 價格 $1.00（中額，走到老闆房門口等你核准）
#   PRICE=9.99 tools/demo_stack.sh      # 超過單筆上限，Deny 落帳
#
# 【為何 Slack/TG 要設成空字串而不是 unset】cogito 自己會 godotenv.Load() 讀 .env，unset 會被它
# 從檔案裡撿回來、然後連上 Slack。godotenv 不覆寫【已存在】的變數，空字串也算存在——所以設空。
# 同理把 Langfuse／OTel 也設空：demo 期間不出本機。
#
# 秘密一律留在各自的 .env（不在這裡）：COGITO_HTTP_TOKEN／COGITO_HTTP_APPROVER_TOKEN／X402_MOCK_SECRET。
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)                 # unity_demo/
COGITO=${COGITO_DIR:-"$HERE/../cogito-agent"}
PRICE=${PRICE:-0.05}
LOGS=${LOGS:-"$HERE/.demo-logs"}; mkdir -p "$LOGS"

[ -f "$COGITO/.env" ] || { echo "找不到 $COGITO/.env（用 COGITO_DIR=... 指定 cogito 目錄）"; exit 1; }
[ -f "$HERE/backend/.env" ] || { echo "找不到 $HERE/backend/.env"; exit 1; }

# x402mock 的簽名秘密要跟 cogito 出納用的一致（都從 cogito 的 .env 讀，沒設就是 demo）
SECRET=$(grep -E '^X402_MOCK_SECRET=' "$COGITO/.env" | cut -d= -f2- | tr -d '"'"'"' ' || true)
SECRET=${SECRET:-demo}

pids=()
cleanup() { echo; echo "收拾中…"; for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "▶ x402mock  :4021  價格 \$$PRICE  → $LOGS/x402mock.log"
( cd "$COGITO" && exec go run ./cmd/x402mock -addr 127.0.0.1:4021 -price "$PRICE" -secret "$SECRET" ) >"$LOGS/x402mock.log" 2>&1 &
pids+=($!)

echo "▶ cogito    office-only（Slack/TG 關）→ $LOGS/cogito.log"
( cd "$COGITO" && \
  SLACK_BOT_TOKEN= SLACK_APP_TOKEN= TELEGRAM_BOT_TOKEN= \
  LANGFUSE_BASE_URL= LANGFUSE_PUBLIC_KEY= LANGFUSE_SECRET_KEY= OTEL_EXPORTER_OTLP_ENDPOINT= \
  exec go run ./cmd/claw ) >"$LOGS/cogito.log" 2>&1 &
pids+=($!)

echo "▶ 橋        :8123  → $LOGS/bridge.log"
( cd "$HERE/backend" && exec ../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8123 ) >"$LOGS/bridge.log" 2>&1 &
pids+=($!)

# 等三個都起來，把該看的 log 行撈出來給人看
for i in $(seq 1 60); do
  a=$(grep -c "x402mock\] 監聽" "$LOGS/x402mock.log" 2>/dev/null || true)
  b=$(grep -c "HTTP 派工入口監聽" "$LOGS/cogito.log" 2>/dev/null || true)
  c=$(grep -c "Uvicorn running\|Application startup complete" "$LOGS/bridge.log" 2>/dev/null || true)
  [ "${a:-0}" -ge 1 ] && [ "${b:-0}" -ge 1 ] && [ "${c:-0}" -ge 1 ] && break
  sleep 1
done
echo
grep -hE "x402mock\] 監聽" "$LOGS/x402mock.log" || echo "⚠ x402mock 還沒起來，看 $LOGS/x402mock.log"
grep -hE "\[policy\] 支付|\[Slack\]|\[office\]" "$LOGS/cogito.log" || echo "⚠ cogito 還沒起來，看 $LOGS/cogito.log"
grep -hE "Uvicorn running" "$LOGS/bridge.log" || echo "⚠ 橋還沒起來，看 $LOGS/bridge.log"
if grep -qE "Socket Mode|Slack 服務已啟動|Telegram 長輪詢" "$LOGS/cogito.log"; then
  echo "⛔ cogito 連上了 Slack/Telegram——這不該發生，請回報"; fi
echo
echo "打開 http://127.0.0.1:8123/shell ，選老徐，派工："
echo "  這個研究任務需要 http://127.0.0.1:4021/premium-data 的資料，請用 request_payment 取得並摘要內容。"
echo "Ctrl-C 收掉三個行程。"
wait

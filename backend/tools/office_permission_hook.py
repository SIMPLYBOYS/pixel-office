#!/usr/bin/env python3
"""Claude Code 的 PermissionRequest hook：把員工 CLI 的權限請求交給辦公室審批。

-p 模式沒有人能回答權限提問，先前一律變成拒絕。這支 hook 由員工 profile（CLAUDE_CONFIG_DIR）的
settings.json 掛上，橋啟動時會自動同步進去。流程：Claude Code 把請求餵到 stdin → 這裡 POST 給橋的
/office/permission → 橋開一張審批卡（倒數、走到老闆房門口、與 cogito 那條同一套 UI）→ 老闆放行或駁回
→ 橋回應 → 這裡印出決定。橋連不上、逾時、任何錯誤都是【拒絕】：無人可審批就不該放行。
只用標準函式庫：這支腳本跑在 Claude Code 的行程樹裡，不能依賴橋的 venv。
"""
import json
import os
import sys
import urllib.request


def office_token() -> str:
    """橋的 token（稽核 #4）：跟橋同一套來源——OFFICE_TOKEN，否則 ~/.pixel-office/token。hook 跑在員工沙箱外，讀得到。"""
    if t := os.environ.get("OFFICE_TOKEN", "").strip():
        return t
    try:
        with open(os.environ.get("OFFICE_TOKEN_FILE") or os.path.expanduser("~/.pixel-office/token"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def decide(behavior: str, message: str = "") -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                             "decision": {"behavior": behavior, "message": message}}},
                     ensure_ascii=False))


def main() -> None:
    try:
        req = json.load(sys.stdin)
    except Exception as e:  # noqa: BLE001 — 任何讀不到都拒絕
        decide("deny", f"hook 讀不到請求：{type(e).__name__}")
        return
    url = os.environ.get("OFFICE_URL", "http://127.0.0.1:8123").rstrip("/") + "/office/permission"
    body = json.dumps({k: req.get(k) for k in ("session_id", "cwd", "tool_name", "tool_input",
                                               "tool_use_id", "permission_mode")}).encode("utf-8")
    wait_s = float(os.environ.get("OFFICE_PERMISSION_WAIT_S", "330"))   # 要比橋的審批逾時（預設 300s）長
    try:
        r = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Office-Token": office_token()})
        with urllib.request.urlopen(r, timeout=wait_s) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        decide("deny", f"辦公室橋連不上或逾時（{type(e).__name__}）——無人可審批，已拒絕")
        return
    decide("allow" if d.get("behavior") == "allow" else "deny", str(d.get("message") or ""))


if __name__ == "__main__":
    main()

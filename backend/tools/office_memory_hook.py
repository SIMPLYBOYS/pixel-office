#!/usr/bin/env python3
"""Claude Code 的 PostToolUse hook：員工寫自己的記憶檔時，通知辦公室落帳。

為什麼需要（2026-09-18 資料留存說明書第 6 類）：員工的 CLI 會自主把事情寫進
`$CLAUDE_CONFIG_DIR/projects/<專案根編碼>/memory/*.md`，那份記憶每次開新 session 都會載入，
但辦公室完全沒有紀錄——抽查一個記憶檔，答不出是誰、哪一張卡、什麼時候寫的。

為什麼是 PostToolUse：記憶已經寫完才有檔名可記，而且不該因為橋沒開就擋住員工寫記憶。
橋連不上就印一行到 stderr（會進 transcript），不改變工具結果、不擋任何事。
只用標準函式庫：這支跑在 Claude Code 的行程樹裡，不能依賴橋的 venv。
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


def main() -> None:
    path = "?"
    try:
        req = json.load(sys.stdin)
        path = str((req.get("tool_input") or {}).get("file_path") or "")
        if "/memory/" not in path or not path.endswith(".md"):
            return          # 不是記憶檔：不表態
        body = json.dumps({"cwd": req.get("cwd"), "session_id": req.get("session_id"),
                           "tool": req.get("tool_name"), "file": path}).encode("utf-8")
        url = os.environ.get("OFFICE_URL", "http://127.0.0.1:8123").rstrip("/") + "/office/memory"
        r = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Office-Token": office_token()})
        with urllib.request.urlopen(r, timeout=5):
            pass
    except Exception as e:  # noqa: BLE001 — 記帳失敗不能影響員工工作，但也不能靜悄悄
        print(f"辦公室記憶落帳失敗（{type(e).__name__}）：{path}", file=sys.stderr)


if __name__ == "__main__":
    main()

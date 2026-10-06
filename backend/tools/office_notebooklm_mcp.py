#!/usr/bin/env python3
"""員工把報告送去 NotebookLM 做語音摘要與簡報——以 MCP 伺服器包住 notebooklm-py 的 CLI。

為什麼不讓員工直接跑 `notebooklm`：那支 CLI 讀 ~/.notebooklm/storage_state.json（老闆 Google 帳號的 cookie），員工沙箱刻意擋住
那個目錄、也沒有網路。這支伺服器跑在沙箱外、以老闆的身分執行 CLI；模型只能叫下面兩個工具，cookie 從頭到尾不經過模型。
同一個模式：jobspy MCP（office_mcp_setup.sh）。

安全邊界（都在這裡驗，不靠模型自律）：
- 只收 OFFICE_WORKSPACE_ROOT 底下的 .md／.txt／.pdf，5 MB 以內；下載也只能落在那底下。
- 每次 publish 開一個新筆記本（標題帶日期），不碰老闆既有的筆記本。
- 只用 argv 呼叫 CLI，不經 shell。

兩個工具、兩段式：生成要幾分鐘，MCP 工具呼叫不該卡那麼久——publish 送件後立刻回 id，collect 問一次「好了沒」，
好了就下載、沒好就回還在等（員工隔一會再叫一次）。只用標準函式庫：跑在 Claude Code 的行程樹裡，不依賴橋的 venv。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("OFFICE_WORKSPACE_ROOT", "")).expanduser()
BIN = os.environ.get("NOTEBOOKLM_BIN", "notebooklm")
LANG = os.environ.get("NOTEBOOKLM_LANGUAGE", "zh_Hant")
OUTPUTS = {"audio": ("audio", ".m4a", [])   # NotebookLM 給的是 MP4 容器的音訊，不是 mp3, "slide-deck": ("slide-deck", ".pptx", ["--format", "pptx"])}
MAX_BYTES = 5 * 1024 * 1024

TOOLS = [
    {"name": "publish",
     "description": "把你工作區裡的一份報告（.md/.txt/.pdf）送去 NotebookLM，開一個新筆記本並開始產生語音摘要與／或簡報。"
                    "立刻回傳 notebook_id 與 artifacts；生成要幾分鐘，之後用 collect 取回。",
     "inputSchema": {"type": "object", "required": ["file"], "properties": {
         "file": {"type": "string", "description": "報告的絕對路徑，必須在你的工作區裡"},
         "title": {"type": "string", "description": "筆記本標題（預設用檔名）"},
         "outputs": {"type": "array", "items": {"type": "string", "enum": list(OUTPUTS)}, "description": "要產生什麼，預設兩種都要"},
         "instructions": {"type": "string", "description": "給生成的指示，例如聽眾是誰、重點放哪"}}}},
    {"name": "collect",
     "description": "問 publish 開出的 artifacts 好了沒；全部完成就下載到 out_dir（必須在你的工作區裡）並回傳檔案路徑，"
                    "還沒好就回 pending，隔一兩分鐘再叫一次。",
     "inputSchema": {"type": "object", "required": ["notebook_id", "artifacts", "out_dir"], "properties": {
         "notebook_id": {"type": "string"},
         "artifacts": {"type": "object", "description": "publish 回傳的 artifacts（類型 → artifact id）"},
         "out_dir": {"type": "string", "description": "下載到哪個資料夾（絕對路徑）"},
         "basename": {"type": "string", "description": "檔名主幹，預設 notebooklm-<日期>"}}}},
]


class ToolError(Exception):
    pass


def inside_root(p: str, kind: str) -> Path:
    if not ROOT.is_dir():
        raise ToolError("伺服器沒設 OFFICE_WORKSPACE_ROOT，拒絕所有檔案操作")
    try:
        path = Path(p).expanduser().resolve()
        path.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        raise ToolError(f"{kind}必須在工作區 {ROOT} 底下：{p}")
    return path


def cli(*args: str, timeout: int = 180) -> dict:
    """跑一次 CLI，回 JSON。非零退出碼：CLI 的 JSON 錯誤照傳（例如額度用完），不是 JSON 就把 stderr 帶出來。"""
    r = subprocess.run([BIN, *args, "--json"], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "NO_COLOR": "1"}, stdin=subprocess.DEVNULL)
    out = r.stdout.strip()
    try:
        data = json.loads(out[out.index("{"):]) if "{" in out else {}
    except ValueError:
        data = {}
    if r.returncode != 0 and not data:
        raise ToolError(f"notebooklm {' '.join(args[:2])} 失敗（退出碼 {r.returncode}）：{(r.stderr or out)[-300:]}")
    if data.get("error") is True or data.get("code"):   # CLI 的錯誤信封是 {"error": true, "code", "message"}；
        raise ToolError(f"notebooklm {' '.join(args[:2])}：{data.get('code')} {data.get('message')}")   # artifact wait 逾時的 "error" 只是說明文字
    return data


def publish(a: dict) -> dict:
    src = inside_root(str(a.get("file") or ""), "報告")
    if not src.is_file():
        raise ToolError(f"找不到這個檔：{src}")
    if src.suffix.lower() not in (".md", ".txt", ".pdf"):
        raise ToolError("只收 .md／.txt／.pdf 的檔案")
    if src.stat().st_size > MAX_BYTES:
        raise ToolError("檔案超過 5 MB")
    outputs = [o for o in (a.get("outputs") or list(OUTPUTS)) if o in OUTPUTS] or list(OUTPUTS)
    title = (str(a.get("title") or src.stem).strip()[:120]) + time.strftime("（%Y-%m-%d）")
    nb = cli("create", title)["notebook"]["id"]
    s = cli("source", "add", str(src), "-n", nb, "--title", src.name)["source"]["id"]
    cli("source", "wait", s, "-n", nb, "--timeout", "120")
    arts = {}
    for o in outputs:
        args = ["generate", OUTPUTS[o][0], "-n", nb, "--no-wait", "--language", LANG]
        if a.get("instructions"):
            args.append(str(a["instructions"])[:1000])
        r = cli(*args, timeout=300)
        arts[o] = r.get("task_id") or r.get("artifact_id") or ""
    return {"notebook_id": nb, "title": title, "source_id": s, "artifacts": arts,
            "next": "生成要幾分鐘：隔一兩分鐘用 collect（帶這個 notebook_id 與 artifacts）取回"}


def collect(a: dict) -> dict:
    nb = str(a.get("notebook_id") or "")
    arts = a.get("artifacts") if isinstance(a.get("artifacts"), dict) else {}
    if not nb or not arts:
        raise ToolError("要帶 publish 回傳的 notebook_id 與 artifacts")
    out_dir = inside_root(str(a.get("out_dir") or ""), "下載資料夾")
    base = str(a.get("basename") or time.strftime("notebooklm-%Y-%m-%d")).strip().replace("/", "-")[:80]
    status = {}
    for o, aid in arts.items():
        if o not in OUTPUTS or not aid:
            status[o] = "unknown"
            continue
        r = cli("artifact", "wait", str(aid), "-n", nb, "--timeout", "5", "--interval", "2")
        status[o] = str(r.get("status") or "unknown")
    if any(s not in ("completed",) for s in status.values()):
        failed = {o: s for o, s in status.items() if s in ("failed", "unknown")}
        return {"ready": False, "status": status, "note": ("有的失敗了，其餘還在生成" if failed else "還在生成中，隔一兩分鐘再叫一次")}
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for o, aid in arts.items():
        sub, ext, extra = OUTPUTS[o]
        dest = out_dir / f"{base}{ext}"
        cli("download", sub, str(dest), "-n", nb, "-a", str(aid), "--force", *extra, timeout=300)
        files[o] = str(dest)
    return {"ready": True, "status": status, "files": files}


def handle(req: dict) -> dict | None:
    m, p, rid = req.get("method"), req.get("params") or {}, req.get("id")
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": p.get("protocolVersion") or "2024-11-05",
                                                       "capabilities": {"tools": {}}, "serverInfo": {"name": "office-notebooklm", "version": "1"}}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if m == "tools/call":
        fn = {"publish": publish, "collect": collect}.get(str(p.get("name")))
        try:
            if fn is None:
                raise ToolError(f"沒有這個工具：{p.get('name')}")
            body, err = fn(p.get("arguments") or {}), False
        except (ToolError, subprocess.TimeoutExpired, KeyError, TypeError) as e:
            body, err = {"error": str(e) if not isinstance(e, KeyError) else f"CLI 回傳少了欄位 {e}"}, True
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": err}}
    if rid is None:   # notifications（initialized 之類）不回
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {m}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if resp := handle(req):
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

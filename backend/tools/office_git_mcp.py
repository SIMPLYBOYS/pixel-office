#!/usr/bin/env python3
"""員工把 worktree 分支提交成 GitHub PR——MCP 伺服器 `office_git`，只有一顆工具 open_pull_request。

為什麼要包一層：員工的 Bash 在沙箱裡、沒有網路，也讀不到老闆的 gh 登入；而 push 與開 PR 是對外的動作。
這支伺服器跑在沙箱外、以老闆的 git／gh 身分執行，但只做一件事，邊界都在這裡驗、不靠模型自律：
- repo_dir 必須在 OFFICE_WORKSPACE_ROOT（員工工作區）底下，而且是一個 git worktree 的根目錄；
- 目前分支必須是 office/…（橋開 worktree 時取的名字）——不碰 main、不碰老闆自己的分支；
- 追蹤中的檔都已 commit、而且比 origin 的預設分支多出 commit；只 push 這一條分支，不 force（沒追蹤的檔不進 PR，列在回傳裡）；
- 同一條分支已經有 PR 就回那一個，不重開。
【刻意不放進常駐許可】：每次呼叫都會經過辦公室的審批（PermissionRequest hook → 審批卡），老闆按准才真的開。
只用標準函式庫：跑在 Claude Code 的行程樹裡，不依賴橋的 venv。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("OFFICE_WORKSPACE_ROOT", "")).expanduser()
GH = os.environ.get("OFFICE_GH_CMD", "gh")
BRANCH_RE = re.compile(r"office/[A-Za-z0-9._-]+")
TITLE_MAX, BODY_MAX = 120, 20000

TOOLS = [{
    "name": "open_pull_request",
    "description": "把你在 worktree 分支（office/…）上已經 commit 的成果推上去，對 repo 的預設分支開一個 PR，回傳 PR 網址。"
                   "會先跳審批給老闆，老闆准了才會開。前提：改動都已 commit、工作樹乾淨。",
    "inputSchema": {"type": "object", "required": ["repo_dir", "title", "body"], "properties": {
        "repo_dir": {"type": "string", "description": "worktree 的絕對路徑（任務描述裡【工作 repo】那個目錄）"},
        "title": {"type": "string", "description": f"PR 標題，{TITLE_MAX} 字內"},
        "body": {"type": "string", "description": "PR 說明（Markdown）：做了什麼、怎麼分工、怎麼驗證、沒做什麼"},
        "draft": {"type": "boolean", "description": "開成草稿 PR，預設 false"}}},
}]


class ToolError(Exception):
    pass


def run(argv: list[str], cwd: Path, stdin: str | None = None, timeout: int = 120) -> str:
    r = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        raise ToolError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} 失敗：{(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout.strip()


def worktree(p: str) -> Path:
    if not ROOT.is_dir():
        raise ToolError("伺服器沒設 OFFICE_WORKSPACE_ROOT，拒絕")
    try:
        d = Path(p).expanduser().resolve()
        d.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        raise ToolError(f"{p} 不在員工工作區裡，拒絕")
    if not d.is_dir() or Path(run(["git", "rev-parse", "--show-toplevel"], d)).resolve() != d:
        raise ToolError(f"{p} 不是一個 git worktree 的根目錄")
    return d


def open_pull_request(args: dict) -> dict:
    d = worktree(str(args["repo_dir"]))
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    if not title or len(title) > TITLE_MAX:
        raise ToolError(f"標題要有、而且在 {TITLE_MAX} 字內")
    if len(body) > BODY_MAX:
        raise ToolError(f"說明超過 {BODY_MAX} 字")
    branch = run(["git", "branch", "--show-current"], d)
    if not BRANCH_RE.fullmatch(branch):
        raise ToolError(f"目前分支是「{branch or '（detached）'}」——只能從 office/… 分支開 PR")
    # 只看追蹤中的檔：看板主持人的 board.json／meeting.md／spec.md 跟 worktree 在同一個目錄、刻意不進 repo，
    # 它們不該擋住 PR；但列在回傳裡——漏 commit 的交付物會出現在這張清單上
    if run(["git", "status", "--porcelain", "--untracked-files=no"], d):
        raise ToolError("還有沒 commit 的修改：先 commit（或還原），工作樹乾淨才開 PR")
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], d).splitlines()
    run(["git", "fetch", "-q", "origin"], d)
    try:
        base = run(["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], d).removeprefix("origin/")
    except ToolError:   # 很久以前 clone 的 repo 常沒有 origin/HEAD：問 GitHub 預設分支是哪條
        base = run([GH, "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"], d)
    ahead = int(run(["git", "rev-list", "--count", f"origin/{base}..HEAD"], d) or 0)
    if ahead == 0:
        raise ToolError(f"這條分支沒有比 {base} 多出任何 commit，沒有東西可以開 PR")
    run(["git", "push", "-u", "origin", f"{branch}:{branch}"], d)
    try:   # 同一條分支已經開過：回那一個，不重開
        url = run([GH, "pr", "view", branch, "--json", "url,state", "-q", "select(.state==\"OPEN\") | .url"], d)
    except ToolError:
        url = ""
    created = not url
    if created:
        argv = [GH, "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", "-"]
        url = run(argv + (["--draft"] if args.get("draft") else []), d, stdin=body).splitlines()[-1]
    return {"url": url, "branch": branch, "base": base, "commits": ahead, "created": created,
            "not_in_pr": untracked[:50]}


def handle(req: dict) -> dict | None:
    m, p, rid = req.get("method"), req.get("params") or {}, req.get("id")
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": p.get("protocolVersion") or "2024-11-05",
                                                       "capabilities": {"tools": {}}, "serverInfo": {"name": "office-git", "version": "1"}}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if m == "tools/call":
        try:
            if p.get("name") != "open_pull_request":
                raise ToolError(f"沒有這個工具：{p.get('name')}")
            body, err = open_pull_request(p.get("arguments") or {}), False
        except (ToolError, subprocess.TimeoutExpired, KeyError, ValueError) as e:
            body, err = {"error": str(e) if not isinstance(e, KeyError) else f"少了參數 {e}"}, True
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": err}}
    if rid is None:
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

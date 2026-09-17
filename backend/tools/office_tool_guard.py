#!/usr/bin/env python3
"""Claude Code 的 PreToolUse hook：員工呼叫 jobspy MCP 之前，用白名單檢查參數，不合格就拒絕。

為什麼需要（2026-09-17 安全稽核 Critical #1）：jobspy-mcp-server 把 searchTerm、location 等參數拼進 shell 字串再
execSync，而員工每天讀不受信任的職缺與貼文。提示注入只要讓模型把 `AI Agent $(任意指令)` 當關鍵字送出，
那段指令就在 MCP 行程裡執行——那個行程不在 Claude Code 的 Bash 沙箱裡。

為什麼放在 PreToolUse：危害發生在「帶著參數呼叫工具」那一刻，所以在那一刻把關。PreToolUse 對已經 allow 的工具
每次都會觸發（PermissionRequest 只在需要問權限時才觸發，mcp__jobspy 在 allow 清單裡，那條管不到）。

為什麼是白名單不是黑名單：黑名單要列舉所有壞字元與寫法，列不完；白名單只列好字元——字母、數字（含中文）、空白與
少數標點。不在清單上的欄位、型別不對、超出範圍，一律拒絕。

這是縱深防禦，不是根本修法：根本修法是讓 jobspy 用參數陣列執行、不經 shell（見 docs/security-audit-2026-09-17.md）。
橋啟動時 sync_office_hook() 會把這支掛進員工 profile。只用標準函式庫（跑在 Claude Code 的行程樹裡）。
任何讀不到、解析失敗、意外錯誤都拒絕：無法確認安全就不放行。
"""
import json
import re
import sys

# 文字欄位：Unicode 字母數字（\w 含中文與底線）、空白、. , + # / -。刻意不收 $ ` " ' \ ; | & < > ( ) 與換行——
# 目前 jobspy 用雙引號包參數，雙引號內會作怪的是 $ ` " \，其餘是保守起見一併排除。
TEXT = re.compile(r"[\w .,+#/-]+")
COUNTRY = re.compile(r"[A-Za-z .-]+")
SITES = {"indeed", "linkedin", "zip_recruiter", "glassdoor", "google", "bayt", "naukri", "bdjobs"}
TEXT_FIELDS = {"searchTerm": 80, "location": 80, "googleSearchTerm": 120}
ENUMS = {"jobType": {"fulltime", "parttime", "internship", "contract"},
         "descriptionFormat": {"markdown", "html"}, "format": {"json", "csv"}}
INTS = {"distance": (0, 500), "resultsWanted": (1, 100), "offset": (0, 1000), "hoursOld": (1, 8760),
        "verbose": (0, 2), "timeout": (1000, 600000)}
BOOLS = {"easyApply", "isRemote", "linkedinFetchDescription", "enforceAnnualSalary"}
BOOL_WORDS = {"true", "false", "yes", "no", "1", "0", "on", "off", "y", "n"}
FORBIDDEN = {"proxies", "caCert"}   # 代理網址與憑證檔路徑：辦公室用不到，放行只是多一個攻擊面


def check(tool_input: dict) -> str:
    """回空字串＝通過；否則回拒絕原因（給模型看，說清楚哪個欄位、為什麼）。"""
    if not isinstance(tool_input, dict):
        return "參數不是物件"
    for key, val in tool_input.items():
        if val is None:
            continue
        if key in TEXT_FIELDS:
            if not isinstance(val, str) or not (1 <= len(val) <= TEXT_FIELDS[key]) or not TEXT.fullmatch(val):
                return (f"{key} 只能是 {TEXT_FIELDS[key]} 字以內的文字、數字、空白與 . , + # / -，"
                        "不能有引號、$、反引號、分號、管線、括號等符號")
        elif key == "countryIndeed":
            if not isinstance(val, str) or not (1 <= len(val) <= 40) or not COUNTRY.fullmatch(val):
                return "countryIndeed 只能是英文國名（例：Taiwan）"
        elif key == "siteNames":
            items = val.split(",") if isinstance(val, str) else val
            if not isinstance(items, list) or not items or any(not isinstance(x, str) or x.strip() not in SITES for x in items):
                return f"siteNames 只能是 {', '.join(sorted(SITES))}"
        elif key == "linkedinCompanyIds":
            if isinstance(val, str):
                if not re.fullmatch(r"\d{1,12}(,\d{1,12}){0,19}", val):
                    return "linkedinCompanyIds 只能是逗號分隔的數字"
            elif not (isinstance(val, list) and all(isinstance(x, int) and not isinstance(x, bool) for x in val)):
                return "linkedinCompanyIds 只能是數字清單"
        elif key in ENUMS:
            if val not in ENUMS[key]:
                return f"{key} 只能是 {', '.join(sorted(ENUMS[key]))}"
        elif key in INTS:
            lo, hi = INTS[key]
            if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
                return f"{key} 要是 {lo}～{hi} 的整數"
        elif key in BOOLS:
            if not (isinstance(val, bool) or (isinstance(val, str) and val.lower() in BOOL_WORDS)):
                return f"{key} 要是 true／false"
        elif key in FORBIDDEN:
            if val not in ("", [], False):
                return f"{key} 不允許使用"
        else:
            return f"不認識的參數 {key}"
    return ""


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": f"辦公室工具守門：{reason}。請改用單純的關鍵字重試。"}},
                     ensure_ascii=False))


def main() -> None:
    try:
        req = json.load(sys.stdin)
        if not str(req.get("tool_name") or "").startswith("mcp__jobspy__"):
            return   # 不是守門對象：不表態，照原本的權限流程走
        if why := check(req.get("tool_input")):
            deny(why)
        # 通過：不表態（不印 allow）——不替工具額外放行，原本的 allow／deny 規則照舊生效
    except Exception as e:  # noqa: BLE001 — 無法確認安全就不放行
        deny(f"守門檢查出錯（{type(e).__name__}）")


if __name__ == "__main__":
    main()

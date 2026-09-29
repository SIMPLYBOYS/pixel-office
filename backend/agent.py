"""Agent：人設＋生活模擬的心智（記憶流、人際、行程）＋三種模型呼叫（排行程、兩人對話、反思）。

生活模擬（OFFICE_MODE 不是 projection 時）照 Generative Agents 的骨架，但為了成本改寫：
- 行程一次排三小時，照表走位【不花錢】；只有排行程、一段對話（兩人整段一次寫完）、反思才呼叫模型。
- 模型預設 Haiku 4.5。每次都要結構化的回答，回來的東西一律驗過才用（地點、人名不在清單上就丟掉）。
- 兩條路叫模型：Claude Code CLI（CliBrain，走訂閱額度，預設）或 Anthropic API（API 額度）。
- 記憶的重要度用規則打分，不另外問模型（原作每則記憶問一次，太貴）。
"""
import asyncio
import json
import math
import os
import time
from pathlib import Path

import yaml

LIFE_MODEL = os.environ.get("OFFICE_LIFE_MODEL", "").strip() or "claude-haiku-4-5"
# 每百萬 token 的美元（Haiku 4.5：輸入、輸出、寫快取、讀快取）。換成別的模型要跟著改，預算才算得準。
PRICE = {"in": 1.0, "out": 5.0, "cache_w": 1.25, "cache_r": 0.10}
MEM_MAX = 80        # 記憶流上限：塞滿時先丟瑣事（見 remember）
LINE_MAX = 20       # 一句話上限：泡泡 8 字一行，20 字＝三行
PLAN_MIN, PLAN_MAX = 5, 45   # 行程一段幾分鐘
PLAN_TOTAL = 240             # 一份行程最長幾分鐘
POSES = ["", "看書", "講電話", "打盹"]

LIFE_RULES = """你在一個像素辦公室模擬裡扮演一位員工。這裡演的是【生活】：喝水、聊天、休息、走動、整理桌面、看窗外、伸展。
真正的工作由系統另外派發，會出現在記憶裡（例如「接到工作任務「X」」「完成了工作任務「X」」「老闆驗收通過了「X」」）。
- 工作的事只能提記憶裡真的有的：任務名稱、完成或中斷、老闆的評語。不要編造任何工作內容、進度，
  也不要說系統、專案、客戶的狀況——看畫面的人會把它當成真的。
- 行程裡坐在自己座位的時段，寫成生活的小事（整理桌面、喝咖啡、回訊息、發呆），不要寫成在做某項工作。
- 說話用繁體中文常用字，口語、簡短（一句 20 字內），符合人設的說話風格。"""


def usage_cost(u) -> float:
    """一次呼叫實際花了多少美元（照回應裡的 usage 算，不猜）。"""
    g = lambda k: int(getattr(u, k, 0) or 0)
    return (g("input_tokens") * PRICE["in"] + g("output_tokens") * PRICE["out"]
            + g("cache_creation_input_tokens") * PRICE["cache_w"] + g("cache_read_input_tokens") * PRICE["cache_r"]) / 1e6


class CliLifeError(Exception):
    """CLI 那條路沒拿到答案（逾時、沒登入、額度用完、回的不是 JSON）。cost＝就算失敗也燒掉的 API 等值。"""
    def __init__(self, msg: str, cost: float = 0.0):
        super().__init__(msg)
        self.cost = cost


CLI_TIMEOUT = 120.0


class CliBrain:
    """用 Claude Code CLI（訂閱額度）回答生活模擬的三種問題：一次 `claude -p`、不給任何工具、不接 MCP、
    系統提示換成 LIFE_RULES（Claude Code 自己那份很長）、思考關掉、--json-schema 要結構化輸出、不留 session。
    提示從 stdin 送（人設與記憶可能很長，也不必出現在 ps 上）。
    env 由呼叫端給（main.agent_env()：白名單，ANTHROPIC_* 不在裡面——有 API key 的話 CLI 會改走 API 計費）。
    cwd 要是個空目錄：Claude Code 會從 cwd 往上找 CLAUDE.md 塞進 context。
    花費＝CLI 回報的 total_cost_usd：訂閱不按次計費，這是「換算成 API 會是多少」，拿來當額度的守門數字。
    ponytail: 每次都起一個 Claude Code 行程（約 5–7 秒、每次約 4k token 的固定開銷，Haiku 的快取門檻 4096 構不到）；
    要更省就換 API（OFFICE_LIFE_ENGINE=api）。"""

    def __init__(self, cmd: str, env: dict, cwd: str):
        self.cmd, self.env, self.cwd = cmd, env, cwd

    async def ask(self, prompt: str, tool: dict) -> tuple[dict | None, float]:
        argv = [self.cmd, "-p", "--model", LIFE_MODEL, "--output-format", "json", "--tools", "", "--strict-mcp-config",
                "--system-prompt", LIFE_RULES, "--json-schema", json.dumps(tool["input_schema"], ensure_ascii=False),
                "--no-session-persistence", "--disable-slash-commands", "--settings", '{"alwaysThinkingEnabled": false}']
        proc = await asyncio.create_subprocess_exec(*argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=CLI_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise CliLifeError(f"CLI 超過 {CLI_TIMEOUT:.0f} 秒沒回")
        try:
            d = json.loads(out)
        except ValueError:
            raise CliLifeError(f"CLI 回的不是 JSON（退出碼 {proc.returncode}）：{err.decode(errors='replace')[:200]}")
        cost = float(d.get("total_cost_usd") or 0) if isinstance(d, dict) else 0.0
        if not isinstance(d, dict) or d.get("is_error") or not isinstance(d.get("structured_output"), dict):
            raise CliLifeError(str((d or {}).get("result") or (d or {}).get("subtype") or "沒有結構化輸出")[:200], cost)
        return d["structured_output"], cost


async def ask(client, prompt: str, tool: dict) -> tuple[dict | None, float]:
    """照 tool 的 schema 回答；回 (參數, 花費)。client 是 CliBrain 就走 CLI，否則是 Anthropic SDK 的 client。"""
    if isinstance(client, CliBrain):
        return await client.ask(prompt, tool)
    # Haiku 4.5 不收 effort（會 400），強制工具（tool_choice=tool）照常可用。max_tokens 留寬：碰頂時工具參數會被截斷
    r = await client.messages.create(
        model=LIFE_MODEL, max_tokens=2048, system=LIFE_RULES, tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}])
    args = next((b.input for b in r.content if b.type == "tool_use"), None)
    return (args if isinstance(args, dict) else None), usage_cost(r.usage)


def clip(s, n: int) -> str:
    """剪到 n 字內。模型常寫得比要求長：優先切在標點（切出來的還是一句話），切不到才硬剪、補「…」——
    直接從中間剪會變成「先想好 rol」這種半個字（實測）。"""
    s = str(s or "").strip().replace("\n", " ")
    if len(s) <= n:
        return s
    cut = max(s.rfind(p, 0, n) for p in "，。！？；、,.!?")
    return s[:cut] if cut >= n // 2 else s[:n - 1] + "…"


class Agent:
    def __init__(self, agent_id: str, persona_dir: Path):
        self.id = agent_id
        self.location = "剛進辦公室"
        path = persona_dir / f"{agent_id}.yaml"
        self.persona = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        # 生活模擬的心智（隨 state 持久化）
        self.memory: list[dict] = []           # [{t, text, imp 1–10, kind}]
        self.relations: dict[str, dict] = {}   # 同事 id -> {"score": -5..5, "note": 最近一次的印象}
        self.plan: dict = {}                   # {"at": 開始的 epoch, "steps": [...]}
        self.since_reflect = 0                 # 上次反思後累積的重要度

    @property
    def name(self) -> str:
        return self.persona.get("name", self.id)

    @property
    def role(self) -> str:
        return self.persona.get("role", "員工")

    @property
    def engine(self) -> str:
        """這位員工用哪個引擎跑（persona 的 `engine:`）：cogito（API 計費）或 cli
        （Claude Code，用訂閱額度）。空＝cogito。與 model 同一個道理——引擎是員工的
        長期屬性（研究型的人可以走訂閱省錢、關鍵決策的人走 API），不是每次派工的參數。"""
        return str(self.persona.get("engine") or "").strip().lower()

    @property
    def model(self) -> str:
        """人設明寫的模型（`model:`）；沒寫就是空。各引擎沒指定時用什麼，由 main.py 依引擎決定——
        Claude Code 用 OFFICE_DEFAULT_MODEL（預設 claude-opus-5[1m]）、cogito 交給它自己的設定（或 OFFICE_COGITO_MODEL）、Codex 用它自己的預設。
        2026-09-17 改：先前這裡回一個全辦公室共用的 Claude 預設，cogito 改走 OpenAI 之後，每次派工都送 claude 過去、被靜默忽略。"""
        return str(self.persona.get("model") or "").strip()

    @property
    def profile(self) -> str:
        p = self.persona
        return (f"{self.name}（{self.role}）。個性：{p.get('personality', '')}。"
                f"說話風格：{p.get('style', '')}。習慣：{p.get('habits', '')}")

    # ── 記憶流 ──
    def remember(self, text: str, imp: int = 3, kind: str = "obs") -> None:
        """imp：1＝走到哪、做什麼（瑣事），3＝一般，5＝聊天心得，6–7＝工作與驗收，8＝反思。"""
        self.memory.append({"t": time.time(), "text": text, "imp": imp, "kind": kind})
        if len(self.memory) > MEM_MAX:
            # 塞滿了先丟最舊的瑣事（走到哪、做什麼，imp≤2）；聊天、工作、心得留著。以前是照順序丟最舊的——
            # 實測八成是瑣事，三、四個小時就塞滿，重要的也一起被擠掉，記憶效應只剩幾小時。全是重要的才丟最舊的。
            del self.memory[next((i for i, m in enumerate(self.memory) if m["imp"] <= 2), 0)]
        self.since_reflect += imp

    @property
    def memory_texts(self) -> list[str]:
        return [m["text"] for m in self.memory]

    def recall(self, k: int = 10, about: tuple = ()) -> list[str]:
        """挑 k 則最該想起的：重要度＋新近（六小時衰減）＋跟話題有關（提到那個人）。照時間排回去。"""
        now = time.time()

        def score(m: dict) -> float:
            return m["imp"] + 4 * math.exp(-(now - m["t"]) / 21600) + (3 if any(w and w in m["text"] for w in about) else 0)
        top = sorted(self.memory, key=score, reverse=True)[:k]
        return [m["text"] for m in sorted(top, key=lambda m: m["t"])]

    def feel(self, other: str, delta: int, note: str) -> None:
        r = self.relations.setdefault(other, {"score": 0, "note": ""})
        r["score"] = max(-5, min(5, r["score"] + delta))
        if note:
            r["note"] = note

    def life_state(self) -> dict:
        # name：工位換了人時，存檔裡那份心智是前一個人的——載入時比對，不同就不接
        return {"name": self.name, "mem": self.memory, "rel": self.relations, "plan": self.plan, "since": self.since_reflect}

    def load_life(self, d: dict) -> None:
        self.memory = [m for m in d.get("mem") or [] if isinstance(m, dict) and "text" in m][-MEM_MAX:]
        self.relations = dict(d.get("rel") or {})
        self.plan = dict(d.get("plan") or {})
        self.since_reflect = int(d.get("since") or 0)

    # ── 三種模型呼叫 ──
    async def make_plan(self, client, now: str, places: list[str], mates: dict[str, str]) -> tuple[list | None, float]:
        """排接下來約三小時。places＝講給模型聽的地點名；mates＝同事名 → 我對他的印象。"""
        tool = {"name": "set_plan", "description": "排接下來約三小時的生活行程，依時間先後",
                "input_schema": {"type": "object", "required": ["steps"], "properties": {"steps": {
                    "type": "array", "items": {"type": "object", "required": ["minutes", "place", "activity"], "properties": {
                        "minutes": {"type": "integer", "description": f"這一段幾分鐘（{PLAN_MIN}–{PLAN_MAX}）"},
                        "place": {"type": "string", "enum": places},
                        "activity": {"type": "string", "description": "在做什麼（15 字內，生活的事）"},
                        "pose": {"type": "string", "enum": POSES, "description": "動作（空＝坐著或站著）"},
                        "with": {"type": "string", "enum": [""] + list(mates), "description": "想找誰聊（空＝自己一個人）"},
                        "say": {"type": "string", "description": "開始這段時隨口說的一句（12 字內，多半留空）"}}}}}}}
        # 剪的長度比要求寬一點：要求 15／12 字、容許 20／16——模型常多寫幾個字，差一點就剪掉太可惜
        mates_s = "；".join(f"{n}（{v}）" for n, v in mates.items()) or "（沒有）"
        prompt = (f"現在是 {now}。幫{self.name}排接下來約三小時的生活行程。\n"
                  f"人設：{self.profile}\n可以去的地方：{'、'.join(places)}\n同事：{mates_s}\n"
                  f"記得的事：{'；'.join(self.recall(12)) or '（剛上班）'}\n"
                  "大部分時間在自己的座位；穿插喝水、走動、找同事聊天，有變化、別跟記憶裡剛做過的一樣。")
        args, cost = await ask(client, prompt, tool)
        steps, total = [], 0
        for s in (args or {}).get("steps") or []:
            if not isinstance(s, dict) or s.get("place") not in places or not clip(s.get("activity"), 20):
                continue   # 地點不在清單上＝走不過去，丟掉
            m = max(PLAN_MIN, min(PLAN_MAX, int(s.get("minutes") or PLAN_MIN)))
            if total + m > PLAN_TOTAL:
                break
            total += m
            steps.append({"minutes": m, "place": s["place"], "activity": clip(s.get("activity"), 20),
                          "pose": s.get("pose") if s.get("pose") in POSES else "",
                          "with": s.get("with") if s.get("with") in mates else "",
                          "say": clip(s.get("say"), 16)})
        return steps or None, cost

    async def chat(self, client, other: "Agent", place: str, now: str) -> tuple[dict | None, float]:
        """我和 other 在 place 碰到：整段對話一次寫完，順便寫各自記住什麼、關係變好還是變差。"""
        a, b = self, other
        tool = {"name": "chat", "description": "寫出這段閒聊",
                "input_schema": {"type": "object", "required": ["lines", "a_memory", "b_memory", "closer"], "properties": {
                    "lines": {"type": "array", "items": {"type": "object", "required": ["who", "text"], "properties": {
                        "who": {"type": "string", "enum": [a.name, b.name]},
                        "text": {"type": "string", "description": f"一句話（{LINE_MAX} 字內）"}}}},
                    "a_memory": {"type": "string", "description": f"{a.name}從這段對話記住什麼（30 字內）"},
                    "b_memory": {"type": "string", "description": f"{b.name}從這段對話記住什麼（30 字內）"},
                    "closer": {"type": "integer", "description": "聊完兩人關係：1 更好、0 不變、-1 變差"}}}}

        def side(x: "Agent", y: "Agent") -> str:
            r = x.relations.get(y.id) or {}
            return (f"{x.profile}\n  對{y.name}的印象：{r.get('note') or '還不太熟'}（好感 {r.get('score', 0)}）\n"
                    f"  記得的事：{'；'.join(x.recall(8, about=(y.name,))) or '（沒什麼）'}")
        prompt = (f"現在是 {now}，{a.name}和{b.name}在{place}碰到，聊了幾句。\n{side(a, b)}\n{side(b, a)}\n"
                  f"寫出這段對話：2–6 句、輪流說、每句 {LINE_MAX} 字內，由{a.name}開口。")
        args, cost = await ask(client, prompt, tool)
        if not args:
            return None, cost
        lines = [{"who": x["who"], "text": clip(x.get("text"), LINE_MAX)} for x in args.get("lines") or []
                 if isinstance(x, dict) and x.get("who") in (a.name, b.name) and clip(x.get("text"), LINE_MAX)][:6]
        if len(lines) < 2:
            return None, cost
        closer = args.get("closer") if args.get("closer") in (-1, 0, 1) else 0
        return {"lines": lines, "a_memory": clip(args.get("a_memory"), 30), "b_memory": clip(args.get("b_memory"), 30),
                "closer": closer}, cost

    async def reflect(self, client) -> tuple[list | None, float]:
        """從最近記得的事想出 1–3 個心得（關於自己、同事或辦公室）。"""
        tool = {"name": "reflect", "description": "寫下心得",
                "input_schema": {"type": "object", "required": ["insights"], "properties": {"insights": {
                    "type": "array", "items": {"type": "string", "description": "一個心得（30 字內）"}}}}}
        prompt = (f"人設：{self.profile}\n最近記得的事：{'；'.join(self.recall(20))}\n"
                  f"以{self.name}的角度，從這些事想出 1–3 個心得。")
        args, cost = await ask(client, prompt, tool)
        out = [clip(x, 30) for x in (args or {}).get("insights") or [] if clip(x, 30)][:3]
        return out or None, cost

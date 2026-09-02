"""Agent 大腦：persona + 記憶 + decide()（架構指南第 3 節）。

decide() 用 strict tools + tool_choice=any → 每次必回一個合法動作 JSON，
Unity 直接執行不用防錯。無 API key 或呼叫失敗時由呼叫端退回隨機走動。
decide_reply() 是對話迴圈專用的輕量決策：回一句（reply）或離開（leave）。
"""
from collections import deque
from pathlib import Path

import yaml

MODEL = "claude-opus-4-8"

WORLD_RULES = """你是一個像素風辦公室模擬遊戲裡的員工 NPC。辦公室裡有工位（chair_*，走到就會自動入座）、
飲水機（cooler_1）、印表機（printer_1）、老闆房（boss_1）等地點。

行為準則：
- 像真實上班族一樣過一天：大部分時間在工位工作，偶爾喝水、印文件、走動休息。
- 依你的人設行動，行程要有變化，不要機械地重複同一件事。
- say 的內容會顯示成頭上的對話泡泡，要非常簡短（15 字以內）。
- say 可以指定對象（to）搭話開啟對話，也可以不指定、當成自言自語。
- 不需要每次決策都說話，安靜做事是常態；但偶爾跟同事互動會讓辦公室更有生氣。
"""

# ponytail: system 總長遠低於 opus-4-8 的 4096 token 快取門檻，cache_control 目前不會生效；
# 世界觀寫豐富之後自動開始省錢（見 claude-api skill 的 prompt-caching 說明）


def build_tools(waypoints: list[str], colleagues: list[str]) -> list[dict]:
    """依 Unity 握手的 waypoint 清單 + 同事名單，為單一 agent 生成 strict tools。"""
    say_props: dict = {
        "channel": {"type": "string", "enum": ["public"]},
        "text": {"type": "string"},
    }
    if colleagues:
        say_props["to"] = {
            "type": "string",
            "enum": colleagues,
            "description": "對誰說（指定了就是搭話，會展開對話；省略＝自言自語）",
        }
    return [
        {
            "name": "move_to",
            "description": "走到某個地點（到座位會自動入座）",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {"target": {"type": "string", "enum": waypoints}},
                "required": ["target"],
                "additionalProperties": False,
            },
        },
        {
            "name": "say",
            "description": "說一句非常簡短的話（頭上冒泡泡，15 字以內）",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": say_props,
                "required": ["channel", "text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "idle",
            "description": "原地休息一會，什麼都不做",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


REPLY_TOOLS = [
    {
        "name": "reply",
        "description": "回一句話（15 字以內）",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "leave",
        "description": "不想聊了，結束這段對話",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


class Agent:
    def __init__(self, agent_id: str, persona_dir: Path):
        self.id = agent_id
        self.location = "剛進辦公室"
        self.memory: deque[str] = deque(maxlen=12)
        path = persona_dir / f"{agent_id}.yaml"
        self.persona = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}

    @property
    def name(self) -> str:
        return self.persona.get("name", self.id)

    @property
    def engine(self) -> str:
        """這位員工用哪個引擎跑（persona 的 `engine:`）：cogito（API 計費）或 cli
        （Claude Code，用訂閱額度）。空＝cogito。與 model 同一個道理——引擎是員工的
        長期屬性（研究型的人可以走訂閱省錢、關鍵決策的人走 API），不是每次派工的參數。"""
        return str(self.persona.get("engine") or "").strip().lower()

    @property
    def model(self) -> str:
        """這位員工跑哪個模型（persona 的 `model:`）。空＝用 cogito 的啟動預設。

        模型是【員工的屬性】而不是每次派工的參數：老徐做架構判斷、小樺做資料彙整，
        本來就該是不同等級的模型，而且那是他們的長期特性。放 persona 才會跟著人走
        （改一行 yaml，不動程式），也才在重啟後還在。
        """
        return str(self.persona.get("model") or "").strip()

    @property
    def persona_prompt(self) -> str:
        p = self.persona
        if not p:
            return f"\n你的代號是 {self.id}，一位普通員工。"
        return (
            f"\n你是{p.get('name', self.id)}（{p.get('role', '員工')}）。"
            f"\n個性：{p.get('personality', '')}"
            f"\n說話風格：{p.get('style', '')}"
            f"\n習慣：{p.get('habits', '')}"
        )

    def remember(self, event: str) -> None:
        self.memory.append(event)

    def _system(self) -> list[dict]:
        return [{
            "type": "text",
            "text": WORLD_RULES + self.persona_prompt,
            "cache_control": {"type": "ephemeral"},
        }]

    @property
    def recent(self) -> str:
        return "；".join(self.memory) if self.memory else "（剛上班，還沒做什麼）"

    async def decide(self, client, tools: list[dict], others: str = "") -> list[dict]:
        """例行決策。回傳 [{"action": "move_to", "target": ...}, ...]"""
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=512,
            output_config={"effort": "low"},  # 例行決策用 low（架構指南第 4 節）
            system=self._system(),
            tools=tools,
            tool_choice={"type": "any"},  # 每次必須選一個動作
            messages=[{
                "role": "user",
                "content": (
                    f"同事動態：{others or '不清楚'}\n"
                    f"最近記憶：{self.recent}\n"
                    f"你現在位於：{self.location}\n"
                    "決定接下來要做什麼。同事在的位置不要過去擠。"
                ),
            }],
        )
        return [
            {"action": b.name, **b.input}
            for b in resp.content
            if b.type == "tool_use"
        ]

    async def decide_reply(self, client, other_name: str, line: str) -> dict | None:
        """對話迴圈的輕量決策：對方說了 line，回一句或離開。"""
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=256,
            output_config={"effort": "low"},
            system=self._system(),
            tools=REPLY_TOOLS,
            tool_choice={"type": "any"},
            messages=[{
                "role": "user",
                "content": (
                    f"你正在和{other_name}對話。{other_name}剛對你說：「{line}」\n"
                    f"最近記憶：{self.recent}\n"
                    "用你的說話風格回一句（15 字以內），或不想聊就結束對話。"
                ),
            }],
        )
        for b in resp.content:
            if b.type == "tool_use":
                return {"action": b.name, **b.input}
        return None

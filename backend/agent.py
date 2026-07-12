"""Agent 大腦：persona + 記憶 + decide()（架構指南第 3 節）。

decide() 用 strict tools + tool_choice=any → 每次必回一個合法動作 JSON，
Unity 直接執行不用防錯。無 API key 或呼叫失敗時由呼叫端退回隨機走動。
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
- say 的內容會顯示成頭上的對話泡泡，要非常簡短（15 字以內），像自言自語或打招呼。
- 不需要每次決策都說話，安靜做事是常態。
"""

# ponytail: system 總長遠低於 opus-4-8 的 4096 token 快取門檻，cache_control 目前不會生效；
# 世界觀寫豐富之後自動開始省錢（見 claude-api skill 的 prompt-caching 說明）


def build_tools(waypoints: list[str]) -> list[dict]:
    """依 Unity 握手送來的 waypoint 清單動態生成 strict tools。"""
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
                "properties": {
                    "channel": {"type": "string", "enum": ["public"]},
                    "text": {"type": "string"},
                },
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


class Agent:
    def __init__(self, agent_id: str, persona_dir: Path):
        self.id = agent_id
        self.location = "剛進辦公室"
        self.memory: deque[str] = deque(maxlen=12)
        path = persona_dir / f"{agent_id}.yaml"
        self.persona = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}

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

    async def decide(self, client, tools: list[dict], others: str = "") -> list[dict]:
        """回傳 [{"action": "move_to", "target": ...}, ...]"""
        recent = "；".join(self.memory) if self.memory else "（剛上班，還沒做什麼）"
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=512,
            output_config={"effort": "low"},  # 例行決策用 low（架構指南第 4 節）
            system=[{
                "type": "text",
                "text": WORLD_RULES + self.persona_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=tools,
            tool_choice={"type": "any"},  # 每次必須選一個動作
            messages=[{
                "role": "user",
                "content": (
                    f"同事動態：{others or '不清楚'}\n"
                    f"最近記憶：{recent}\n"
                    f"你現在位於：{self.location}\n"
                    "決定接下來要做什麼。同事在的位置不要過去擠。"
                ),
            }],
        )
        actions = []
        for block in resp.content:
            if block.type == "tool_use":
                actions.append({"action": block.name, **block.input})
        return actions

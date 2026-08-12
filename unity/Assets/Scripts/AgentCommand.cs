// 架構指南第 2 節的通訊合約——假大腦與未來 WebSocket 後端共用同一格式
// { "agent_id": "nina", "action": "move_to", "target": "meeting_room" }
[System.Serializable]
public class AgentCommand
{
    public string agent_id;
    public string action;  // move_to / use / say / focus
    public string target;
    public string channel;
    public string text;
    // focus 專用：要框住的人（空＝回基態全景）與這件事的優先級。
    // 沒有 agent_id——它不是對某個 NPC 下的指令，是對【鏡頭】下的。
    public string[] agents;
    public int level;
}

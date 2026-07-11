// 架構指南第 2 節的通訊合約——假大腦與未來 WebSocket 後端共用同一格式
// { "agent_id": "nina", "action": "move_to", "target": "meeting_room" }
[System.Serializable]
public class AgentCommand
{
    public string agent_id;
    public string action;  // move_to / use / say（post_task 之後接任務板再加）
    public string target;
    public string channel;
    public string text;
}

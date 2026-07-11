using System.Collections;
using UnityEngine;

// 指令執行器：吃 AgentCommand、驅動身體。假大腦與未來的 WebSocket 分發器都呼叫這裡——
// 換真大腦時本類一行不改（筆記 Step 4 的「換心臟不換身體」）
public class NPCAgent : MonoBehaviour
{
    public string agentId;

    NPCMover mover;
    NPCSprite sprite;
    NPCMeeting meeting;

    void Awake()
    {
        mover = GetComponent<NPCMover>();
        sprite = GetComponent<NPCSprite>();
        meeting = GetComponent<NPCMeeting>();
    }

    public IEnumerator Execute(AgentCommand cmd)
    {
        switch (cmd.action)
        {
            case "move_to":
                sprite.SetAction(null);
                var wp = WaypointRegistry.Get(cmd.target);
                if (wp == null)
                {
                    Debug.LogWarning($"{agentId}: 未知 waypoint '{cmd.target}'");
                    yield break;
                }
                mover.MoveTo(wp.pos.position);
                yield return new WaitUntil(() => mover.Arrived);
                // ponytail: 之後接 WebSocket，在這裡回報 {type:"arrived", agent_id, at}
                break;

            case "use": // target = 動作名（sit_up / sit_left / sit_right）
                sprite.SetAction(cmd.target);
                break;

            case "say": // demo 階段用泡泡表示；接 UGUI 聊天面板時改推 cmd.text
                if (meeting != null) yield return meeting.ShowBubble(3f);
                break;
        }
    }

    public void ClearAction() => sprite.SetAction(null);
}

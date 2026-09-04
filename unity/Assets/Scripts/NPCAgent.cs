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
    NPCEmote emote;

    void Awake()
    {
        mover = GetComponent<NPCMover>();
        sprite = GetComponent<NPCSprite>();
        meeting = GetComponent<NPCMeeting>();
        emote = GetComponentInChildren<NPCEmote>(true);   // 徽章掛在子物件上
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
                // 到點自動執行該點動作（入座是身體知識，不需要大腦下指令）
                if (wp.action != null) sprite.SetAction(wp.action);
                break;

            case "use": // target = 動作名（sit_up / sit_left / sit_right）
                sprite.SetAction(cmd.target);
                break;

            case "emote": // target = 徽章名（wait / think / warn / alert），空＝收起來。
                // 【不是】身體姿勢：它跟走位、坐姿完全獨立，所以 move_to 不會清掉它。
                // 狀態還在，人走去飲水機的路上徽章也該還在。
                if (emote != null) emote.Show(cmd.target);
                break;

            case "say": // 有文字顯示文字框，沒文字退回「...」泡泡
                var speech = GetComponent<NPCSpeech>();
                if (speech != null && !string.IsNullOrEmpty(cmd.text))
                    speech.Show(cmd.text);
                else if (meeting != null)
                    yield return meeting.ShowBubble(3f);
                break;
        }
    }

    public void ClearAction() => sprite.SetAction(null);
}

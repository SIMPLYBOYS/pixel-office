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

            case "visible": // target = "0" 藏起來、"1" 顯示（團隊設定：沒啟用的工位，docs/team-setup.md）
                SetVisible(cmd.target != "0");
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

    // 關 renderer 與碰撞，不用 SetActive(false)：物件停用後這個元件也停了，就收不到下一個 "visible 1"。
    // 碰撞一起關——看不見的人不該擋路，也不該被點出報告卡。徽章、文字框都在子物件上，一起收。
    public void SetVisible(bool on)
    {
        foreach (var r in GetComponentsInChildren<Renderer>(true)) r.enabled = on;
        foreach (var c in GetComponentsInChildren<Collider2D>(true)) c.enabled = on;
        foreach (var cv in GetComponentsInChildren<Canvas>(true)) cv.enabled = on;
        if (!on) return;
        // 上面一律打開，會把平常關著的圖層也打開：「...」泡泡（NPCMeeting 用 renderer 開關）會從此掛在頭上，
        // 橋每次畫面連線都送 visible 1，於是全辦公室頭上都是「...」，看起來像在聊天（2026-10-08 實際回報）。
        // 這兩個圖層的開關不歸這裡管：泡泡收回去，徽章照它自己的狀態。
        if (meeting != null && meeting.bubble != null) meeting.bubble.enabled = false;
        if (emote != null) emote.Refresh();
    }
}

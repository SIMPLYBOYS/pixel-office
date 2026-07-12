using System.Collections;
using UnityEngine;

// ponytail: 假大腦——之後換 WebSocket 收指令，Execute() 一行不改（筆記 Step 3 原文）
// 行為：挑一個沒人佔的互動點 → 走過去 → 有動作就做（入座）→ 待 5–15 秒 → 下一站
public class FakeBrain : MonoBehaviour
{
    NPCAgent agent;
    NPCMeeting meeting;

    IEnumerator Start()
    {
        agent = GetComponent<NPCAgent>();
        meeting = GetComponent<NPCMeeting>();
        yield return new WaitForSeconds(Random.Range(0.5f, 2.5f)); // 錯開起步

        while (true)
        {
            // BrainGateway 連上後端時停用本組件 → 迴圈停在這裡；斷線重啟後自動續跑
            while (!enabled || meeting.Busy) yield return null;

            var wp = WaypointRegistry.ClaimRandom(this);
            if (wp == null)
            {
                yield return new WaitForSeconds(2f);
                continue;
            }

            // move_to 到點會自動入座（NPCAgent 的身體知識），不用再下 use
            yield return agent.Execute(new AgentCommand { action = "move_to", target = wp.key });

            float dwell = Random.Range(5f, 15f);
            while (dwell > 0f)
            {
                dwell -= Time.deltaTime;
                yield return null;
            }
            while (meeting.Busy) yield return null;

            agent.ClearAction();
            WaypointRegistry.Release(this);
        }
    }
}

using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using NativeWebSocket;
using UnityEngine;

// Step 4 的心臟接口：連上後端 → 停用假大腦、收 JSON 指令餵給同一個 Execute()；
// 斷線 → 假大腦自動接回。每 3 秒重試連線，先開 Unity 或先開後端都行。
public class BrainGateway : MonoBehaviour
{
    public static bool RemoteMode { get; private set; } // 遠端接管中：相遇等本地自發行為讓位

    public string url = "ws://localhost:8123/ws";

    WebSocket ws;
    Dictionary<string, NPCAgent> agents;
    bool remote;

    void Start()
    {
        agents = FindObjectsByType<NPCAgent>(FindObjectsSortMode.None)
            .ToDictionary(a => a.agentId);
        _ = ConnectLoop();
    }

    async Task ConnectLoop()
    {
        while (Application.isPlaying && this != null)
        {
            ws = new WebSocket(url);
            ws.OnOpen += () =>
            {
                remote = true;
                RemoteMode = true;
                ToggleFakeBrains(false);
                Debug.Log("BrainGateway: 已連上後端，假大腦停用");
            };
            ws.OnClose += _ =>
            {
                if (!remote) return;
                remote = false;
                RemoteMode = false;
                ToggleFakeBrains(true);
                Debug.Log("BrainGateway: 後端斷線，假大腦接回");
            };
            ws.OnMessage += bytes => Dispatch(Encoding.UTF8.GetString(bytes));

            try { await ws.Connect(); } // 連線存續期間都停在這行，關閉/失敗才返回
            catch { /* 後端沒開，稍後重試 */ }

            await Task.Delay(3000);
        }
    }

    void Update()
    {
#if !UNITY_WEBGL || UNITY_EDITOR
        ws?.DispatchMessageQueue();
#endif
    }

    void Dispatch(string json)
    {
        var cmd = JsonUtility.FromJson<AgentCommand>(json);
        if (cmd == null || string.IsNullOrEmpty(cmd.agent_id)) return;
        if (!agents.TryGetValue(cmd.agent_id, out var npc))
        {
            Debug.LogWarning($"BrainGateway: 未知 agent_id '{cmd.agent_id}'");
            return;
        }
        StartCoroutine(Run(npc, cmd));
    }

    IEnumerator Run(NPCAgent npc, AgentCommand cmd)
    {
        yield return npc.Execute(cmd);
        if (cmd.action == "move_to")
            Send($"{{\"type\":\"arrived\",\"agent_id\":\"{npc.agentId}\",\"at\":\"{cmd.target}\"}}");
    }

    async void Send(string json)
    {
        if (ws != null && ws.State == WebSocketState.Open) await ws.SendText(json);
    }

    void ToggleFakeBrains(bool on)
    {
        foreach (var b in FindObjectsByType<FakeBrain>(FindObjectsInactive.Include, FindObjectsSortMode.None))
            b.enabled = on; // FakeBrain 迴圈頂端會自己停在 !enabled
    }

    async void OnApplicationQuit()
    {
        if (ws != null) await ws.Close();
    }
}

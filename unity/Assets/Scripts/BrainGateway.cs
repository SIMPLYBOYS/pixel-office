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
            // WebGL 的 Connect() 立刻返回（套件實作直接回 Task.CompletedTask），不像其他平台會停到斷線。
            // 沒有這個等待，這個迴圈每 3 秒就多開一條連線、舊的沒關，後端每次廣播同一畫面就重複執行（codex review 抓到）。
            // 所以不靠 Connect() 的返回時機，改等 OnClose：兩種平台都成立。
            var closed = new TaskCompletionSource<bool>();
            ws.OnClose += _ => closed.TrySetResult(true);
            ws.OnOpen += () =>
            {
                remote = true;
                RemoteMode = true;
                ToggleFakeBrains(false);
                SendHandshake(); // 告訴後端有哪些 agent 和互動點 → 後端動態生成 tool enum
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

            try
            {
                await ws.Connect();   // 非 WebGL：停到斷線才返回；WebGL：立刻返回，靠下一行等
                await closed.Task;    // 兩種平台都在這裡等到這條連線真的關掉，才進入重連
            }
            catch { /* 後端沒開（Connect 丟例外），稍後重試 */ }

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
        if (cmd == null) return;
        // focus 是對【鏡頭】下的指令，沒有 agent_id——要在下面那個檢查之前接掉。
        if (cmd.action == "focus")
        {
            var dir = Camera.main != null ? Camera.main.GetComponent<CameraDirector>() : null;
            if (dir == null) return;   // 還沒 Build Room（相機上沒掛導演）就當沒這回事
            var who = (cmd.agents ?? new string[0])
                .Where(agents.ContainsKey).Select(k => agents[k].transform).ToArray();
            dir.Focus(who, cmd.level > 0 ? cmd.level : CameraDirector.LvDispatch);
            return;
        }
        if (string.IsNullOrEmpty(cmd.agent_id)) return;
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

    void SendHandshake()
    {
        var ids = string.Join(",", agents.Keys.Select(k => $"\"{k}\""));
        var wps = string.Join(",", WaypointRegistry.All().Select(e => $"\"{e.key}\""));
        Send($"{{\"type\":\"waypoints\",\"agents\":[{ids}],\"list\":[{wps}]}}");
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

using System.Collections;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.Networking;

// 點 NPC 查看他最近一次工作任務的報告全文（橋端 GET /office/report/<id>）。
// RuntimeInitializeOnLoadMethod 自我安裝——場景零改動、不需重跑 builder。
// 面板走 OnGUI：demo 工具等級的 UI，夠用就好；美化等正式 UI 系統進場再說。
public class ReportViewer : MonoBehaviour
{
    const string BaseUrl = "http://localhost:8123"; // 與 BrainGateway.url 同一後端

    [System.Serializable]
    class Report
    {
        public bool ok;
        public string error, agent, name, task, status, report, at;
    }

    Report current;
    Vector2 scroll;
    GUIStyle boxStyle, titleStyle, bodyStyle;

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    static void Boot() => new GameObject("ReportViewer").AddComponent<ReportViewer>();

    void Update()
    {
        var kb = Keyboard.current;
        if (kb != null && kb.escapeKey.wasPressedThisFrame) current = null;

        var mouse = Mouse.current;
        if (mouse == null || !mouse.leftButton.wasPressedThisFrame) return;
        var world = Camera.main.ScreenToWorldPoint(mouse.position.ReadValue());
        foreach (var hit in Physics2D.OverlapPointAll(world))
        {
            var npc = hit.GetComponentInParent<NPCAgent>();
            if (npc == null) continue;
            StartCoroutine(Fetch(npc.name.Replace("NPC_", "")));
            return;
        }
        current = null; // 點空白處關閉
    }

    IEnumerator Fetch(string id)
    {
        using var req = UnityWebRequest.Get($"{BaseUrl}/office/report/{id}");
        yield return req.SendWebRequest();
        current = req.result == UnityWebRequest.Result.Success
            ? JsonUtility.FromJson<Report>(req.downloadHandler.text)
            : new Report { ok = false, error = "後端未連線" };
    }

    static string StatusLabel(string s) => s switch
    {
        "working" => "⏳ 進行中",
        "ok" => "✔ 已完成",
        "error" => "✗ 失敗",
        "lost" => "⚠ 失聯中斷",
        _ => s,
    };

    void EnsureStyles()
    {
        if (boxStyle != null) return;
        var cjk = Font.CreateDynamicFontFromOSFont("PingFang TC", 15);
        var bg = new Texture2D(1, 1);
        bg.SetPixel(0, 0, new Color(0.08f, 0.09f, 0.12f, 0.94f));
        bg.Apply();
        boxStyle = new GUIStyle { padding = new RectOffset(16, 16, 12, 12) };
        boxStyle.normal.background = bg;
        titleStyle = new GUIStyle { font = cjk, fontSize = 17, wordWrap = true };
        titleStyle.normal.textColor = Color.white;
        bodyStyle = new GUIStyle { font = cjk, fontSize = 14, wordWrap = true, richText = false };
        bodyStyle.normal.textColor = new Color(0.85f, 0.87f, 0.9f);
    }

    void OnGUI()
    {
        if (current == null) return;
        EnsureStyles();
        float w = Mathf.Min(560, Screen.width - 40);
        float h = Mathf.Min(420, Screen.height - 60);
        GUILayout.BeginArea(new Rect((Screen.width - w) / 2, 30, w, h), boxStyle);
        if (current.ok)
        {
            GUILayout.Label($"{current.name}　{StatusLabel(current.status)}　{current.at}", titleStyle);
            GUILayout.Space(4);
            GUILayout.Label($"任務：{current.task}", bodyStyle);
            GUILayout.Space(8);
            scroll = GUILayout.BeginScrollView(scroll);
            GUILayout.Label(string.IsNullOrEmpty(current.report) ? "（還沒有報告內容）" : current.report, bodyStyle);
            GUILayout.EndScrollView();
        }
        else
        {
            GUILayout.Label(current.error, titleStyle);
        }
        GUILayout.Space(6);
        GUILayout.Label("Esc 或點空白處關閉", bodyStyle);
        GUILayout.EndArea();
    }
}

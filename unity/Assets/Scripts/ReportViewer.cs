using System.Collections;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.Networking;

// 點 NPC 開右側工作欄：任務、狀態、逐步事件時間軸（對齊 Slack 的資訊量）、報告全文。
// 工作中每 2 秒自動刷新（像即時進度串）。RuntimeInitializeOnLoadMethod 自我安裝，場景零改動。
// 面板走 OnGUI（demo 工具等級）；Pixffice 式 Web 外殼是下一個里程碑，這裡不重造。
public class ReportViewer : MonoBehaviour
{
    const string BaseUrl = "http://localhost:8123"; // 與 BrainGateway.url 同一後端
    const float Refresh = 2f;

    [System.Serializable]
    class Entry { public string at, text; }

    [System.Serializable]
    class Report
    {
        public bool ok;
        public string error, agent, name, task, status, report, at;
        public Entry[] timeline;
    }

    Report current;
    string watching; // 正在追蹤的 agent id；null＝面板關閉
    int lastCount = -1;
    Vector2 scroll;
    GUIStyle boxStyle, titleStyle, bodyStyle, dimStyle;

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    static void Boot() => new GameObject("ReportViewer").AddComponent<ReportViewer>();

    void Update()
    {
        var kb = Keyboard.current;
        if (kb != null && kb.escapeKey.wasPressedThisFrame) Close();

        var mouse = Mouse.current;
        if (mouse == null || !mouse.leftButton.wasPressedThisFrame) return;
        var world = Camera.main.ScreenToWorldPoint(mouse.position.ReadValue());
        foreach (var hit in Physics2D.OverlapPointAll(world))
        {
            var npc = hit.GetComponentInParent<NPCAgent>();
            if (npc == null) continue;
            var id = npc.name.Replace("NPC_", "");
            if (watching != id) { Close(); StartCoroutine(Watch(id)); }
            return;
        }
        Close(); // 點空白處關閉
    }

    void Close() { watching = null; current = null; lastCount = -1; }

    IEnumerator Watch(string id)
    {
        watching = id;
        while (watching == id)
        {
            yield return Fetch(id);
            yield return new WaitForSeconds(Refresh); // 工作中面板跟著事件長
        }
    }

    IEnumerator Fetch(string id)
    {
        using var req = UnityWebRequest.Get($"{BaseUrl}/office/report/{id}");
        yield return req.SendWebRequest();
        if (watching != id) yield break; // 期間已切人/關閉
        current = req.result == UnityWebRequest.Result.Success
            ? JsonUtility.FromJson<Report>(req.downloadHandler.text)
            : new Report { ok = false, error = "後端未連線" };
        int n = current.timeline?.Length ?? 0;
        if (n != lastCount) { scroll.y = float.MaxValue; lastCount = n; } // 有新事件才跳到底
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
        var cjk = Font.CreateDynamicFontFromOSFont("PingFang TC", 18);
        var bg = new Texture2D(1, 1);
        bg.SetPixel(0, 0, new Color(0.08f, 0.09f, 0.12f, 0.96f));
        bg.Apply();
        boxStyle = new GUIStyle { padding = new RectOffset(20, 20, 16, 16) };
        boxStyle.normal.background = bg;
        titleStyle = new GUIStyle { font = cjk, fontSize = 21, wordWrap = true };
        titleStyle.normal.textColor = Color.white;
        bodyStyle = new GUIStyle { font = cjk, fontSize = 16, wordWrap = true, richText = false };
        bodyStyle.normal.textColor = new Color(0.85f, 0.87f, 0.9f);
        dimStyle = new GUIStyle(bodyStyle) { fontSize = 13 };
        dimStyle.normal.textColor = new Color(0.55f, 0.58f, 0.63f);
    }

    void OnGUI()
    {
        if (current == null) return;
        EnsureStyles();
        // OnGUI 是裸像素座標，Retina/高解析 Game view 不會自動縮放——按畫面高度
        // 整體縮放（720 為基準），滑鼠輸入 IMGUI 會自動換算。
        float s = Mathf.Max(1f, Screen.height / 720f);
        GUI.matrix = Matrix4x4.Scale(new Vector3(s, s, 1f));
        float sw = Screen.width / s, sh = Screen.height / s;
        float w = Mathf.Min(400, sw * 0.4f);
        GUILayout.BeginArea(new Rect(sw - w, 0, w, sh), boxStyle); // 右側固定欄
        if (current.ok)
        {
            GUILayout.Label($"{current.name}　{StatusLabel(current.status)}", titleStyle);
            GUILayout.Label($"任務 · {current.at}", dimStyle);
            GUILayout.Label(current.task, bodyStyle);
            GUILayout.Space(8);
            scroll = GUILayout.BeginScrollView(scroll);
            if (current.timeline != null)
                foreach (var e in current.timeline)
                {
                    GUILayout.Label(e.at, dimStyle);
                    GUILayout.Label(e.text, bodyStyle);
                    GUILayout.Space(4);
                }
            if (!string.IsNullOrEmpty(current.report))
            {
                GUILayout.Space(8);
                GUILayout.Label("── 報告全文 ──", dimStyle);
                GUILayout.Label(current.report, bodyStyle);
            }
            GUILayout.EndScrollView();
        }
        else
        {
            GUILayout.Label(current.error, titleStyle);
        }
        GUILayout.Label("Esc 或點空白處關閉", dimStyle);
        GUILayout.EndArea();
    }
}

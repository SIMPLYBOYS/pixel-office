using UnityEngine;
using UnityEngine.InputSystem;
#if UNITY_WEBGL && !UNITY_EDITOR
using System.Runtime.InteropServices;
#endif

// WebGL 專用：點畫布裡的 NPC → 通知外層 Web 外殼選中該員工（右欄跟著切）。
// 桌面版對應功能是 ReportViewer 的 OnGUI 面板；兩者互斥安裝，各自只有點擊偵測那幾行重複。
public class ShellBridge : MonoBehaviour
{
#if UNITY_WEBGL && !UNITY_EDITOR
    [DllImport("__Internal")]
    static extern void OfficeSelectAgent(string id);

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    static void Boot() => new GameObject("ShellBridge").AddComponent<ShellBridge>();

    void Update()
    {
        var mouse = Mouse.current;
        if (mouse == null || !mouse.leftButton.wasPressedThisFrame) return;
        var world = Camera.main.ScreenToWorldPoint(mouse.position.ReadValue());
        foreach (var hit in Physics2D.OverlapPointAll(world))
        {
            var npc = hit.GetComponentInParent<NPCAgent>();
            if (npc == null) continue;
            OfficeSelectAgent(npc.name.Replace("NPC_", ""));
            return;
        }
    }
#endif
}

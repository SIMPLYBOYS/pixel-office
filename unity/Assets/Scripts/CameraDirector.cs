using System.Linq;
using UnityEngine;

// 相機導演：三態 + 優先級仲裁。規格出自團隊自己開的那場會（meeting-camera-follow.md）：
// 地圖從 16 格寬變成 29 格之後，A（縮小看全景）與 B（相機跟事件走）二選一，裁定選 B——
// 「產品的命是看清每個 agent 在幹嘛的細節，細節是投影誠實的載體；縮四成的全景會糊掉細節＝假全景」。
//
// 三態：
//   基態  全景俯瞰。無事件時待在這裡——不主張細節，所以不算假。
//   跟隨  有【夠份量】的事件才推近該區。
//   鎖定  老闆手動聚焦；只有他能解。
//
// ⚠ 禁止「回到地圖中心 + size 9」那個舊行為——那正是「兩邊都只看得到一半」的病灶。
// 基態是【框住整張地圖】，不是固定的座標與倍率。
public class CameraDirector : MonoBehaviour
{
    // 優先級階梯（數字小＝優先）。每一級都對得上真實事件源；
    // 刻意不為了「讓相機有事做」而降門檻——⑥ 級（一般工具呼叫、學到記憶）根本不觸發移動。
    public const int LvManual = 1;   // 老闆手動聚焦（鎖）
    public const int LvDecision = 2; // 需老闆決策：成本熔斷／等審批／等輸入
    public const int LvMeeting = 3;  // 開會，多人聚集
    public const int LvFailure = 4;  // 失敗、異常
    public const int LvDispatch = 5; // 剛派工／剛完成
    public const int LvNone = 6;     // 不觸發移動

    [Header("取景")]
    public float overviewPad = 1f;    // 全景的邊距（世界單位）
    public float closeupSize = 5.5f;  // 推近時的 orthographic size
    public float panSpeed = 3.5f;     // 平移/縮放的跟隨速度

    [Header("紀律")]
    public float cooldown = 4f;       // 冷卻窗：窗內只有【更高】優先能搶鏡
    public float idleReturn = 14f;    // 這麼久沒有夠份量的事件就回基態

    [Header("地圖範圍（RoomBuilder 建房時填）")]
    public Vector2 mapMin = new(0f, -17f);
    public Vector2 mapMax = new(29f, 0f);

    Camera cam;
    int level = LvNone;               // 目前這個取景的優先級
    float since;                      // 上次接受取景的時間
    Vector3 wantPos;
    float wantSize;

    void Awake()
    {
        cam = GetComponent<Camera>();
        Overview();
        // 開場直接就位，不要從舊座標慢慢飄過去
        transform.position = wantPos;
        cam.orthographicSize = wantSize;
    }

    /// 回基態：框住整張地圖。
    public void Overview()
    {
        var c = (mapMin + mapMax) / 2f;
        float w = (mapMax.x - mapMin.x) / 2f + overviewPad;
        float h = (mapMax.y - mapMin.y) / 2f + overviewPad;
        wantPos = new Vector3(c.x, c.y, transform.position.z);
        // 寬度受 aspect 影響：同一個 size 在窄畫面看得比較少，所以兩個方向都要滿足
        wantSize = Mathf.Max(h, w / Mathf.Max(cam.aspect, 0.1f));
        level = LvNone;
        since = Time.time;
    }

    /// 推近一群人。回傳是否接受——被紀律擋下來時回 false（呼叫端不必知道為什麼）。
    public bool Focus(Transform[] who, int lv)
    {
        if (who == null || who.Length == 0)
        {
            if (level == LvManual && lv != LvManual) return false;  // 鎖定中只有老闆能解
            Overview();
            return true;
        }
        if (!Allowed(lv)) return false;

        // 取【重心】而不是「涵蓋最多人的折衷取景」——後者會製造一個誰都不在的假中心。
        var c = who.Aggregate(Vector3.zero, (a, t) => a + t.position) / who.Length;
        wantPos = new Vector3(c.x, c.y, transform.position.z);
        // 一群人要框得下：取最遠的那個，再留一點邊
        float need = who.Max(t => Mathf.Max(Mathf.Abs(t.position.y - c.y),
                                            Mathf.Abs(t.position.x - c.x) / Mathf.Max(cam.aspect, 0.1f)));
        wantSize = Mathf.Clamp(need + 2.5f, closeupSize, MaxSize());
        level = lv;
        since = Time.time;
        return true;
    }

    /// 什麼時候相機【不該】動——移動本身也是一種投影，搶走老闆正在看的視角比不動更煩。
    bool Allowed(int lv)
    {
        if (level == LvManual) return lv == LvManual;      // 手動聚焦＝全鎖，只有他能解
        if (lv >= level && Time.time - since < cooldown)   // 冷卻窗內只有【更高】優先能搶鏡
            return false;                                  // 過期即丟，不排隊、不追歷史
        return true;
    }

    float MaxSize()
    {
        float h = (mapMax.y - mapMin.y) / 2f + overviewPad;
        return Mathf.Max(h, ((mapMax.x - mapMin.x) / 2f + overviewPad) / Mathf.Max(cam.aspect, 0.1f));
    }

    void LateUpdate()
    {
        // 沒有夠份量的事件就回基態。手動聚焦不受影響——那是老闆自己選的，不該被時間解掉。
        if (level != LvManual && level != LvNone && Time.time - since > idleReturn)
            Overview();

        float k = 1f - Mathf.Exp(-panSpeed * Time.deltaTime);   // 與幀率無關的平滑
        transform.position = Vector3.Lerp(transform.position, wantPos, k);
        cam.orthographicSize = Mathf.Lerp(cam.orthographicSize, wantSize, k);
    }
}

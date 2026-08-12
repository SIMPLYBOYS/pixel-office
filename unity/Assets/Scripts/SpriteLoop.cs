using UnityEngine;

// 定點循環播放一組幀、偶爾插播另一組。給「不是 agent 投影」的常駐角色用（總機小姐）——
// 她不掛 NPCSprite/NPCMover 那一整套：那套的每個狀態都對應真實 agent 狀態
// （走位、入座、講電話），而她沒有任何狀態可投影。
//
// ⚠ 插播（接電話）是【裝飾】，不是投影——跟自動門同一級：娛樂效果大於實質，
// Aaron 裁定走純隨機（option A）。別把她的接聽讀成「有事進來了」；哪天要改成
// 「派工進來→總機接聽」的真投影（option B），觸發點換成 bridge 的事件即可，這裡不用大改。
public class SpriteLoop : MonoBehaviour
{
    public Sprite[] frames;                      // 待機主迴圈
    public Sprite[] extra;                       // 偶爾插播的動作；留空＝純待機
    public float fps = 6f;
    public Vector2 extraEvery = new(25f, 70f);   // 兩次插播的間隔（秒，區間內隨機）
    public Vector2 extraFor = new(4f, 8f);       // 一次插播持續多久

    SpriteRenderer sr;
    float busyUntil, nextAt;

    void Awake()
    {
        sr = GetComponent<SpriteRenderer>();
        nextAt = Time.time + Random.Range(extraEvery.x, extraEvery.y);
    }

    void Update()
    {
        var set = frames;
        if (extra != null && extra.Length > 0)
        {
            if (Time.time >= nextAt)
            {
                busyUntil = Time.time + Random.Range(extraFor.x, extraFor.y);
                nextAt = busyUntil + Random.Range(extraEvery.x, extraEvery.y);
            }
            if (Time.time < busyUntil) set = extra;
        }
        if (set != null && set.Length > 0)
            sr.sprite = set[(int)(Time.time * fps) % set.Length];
    }
}

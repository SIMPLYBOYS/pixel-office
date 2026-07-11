using System.Collections;
using UnityEngine;

// 相遇規則（筆記 Step 3）：兩 NPC 距離 < 1 tile → 停下互相面對、冒泡泡幾秒、散開
// 偵測用第二顆 trigger 圓（半徑 0.9）；NPC 實體圓之間互不碰撞（走廊窄，推擠會卡）
public class NPCMeeting : MonoBehaviour
{
    public SpriteRenderer bubble;     // CharacterBuilder 指定
    public CircleCollider2D body;     // 實體碰撞圓（供 NPC 間互相忽略）

    public bool Busy { get; private set; }

    float cooldownUntil;
    NPCMover mover;
    NPCSprite sprite;

    void Awake()
    {
        mover = GetComponent<NPCMover>();
        sprite = GetComponent<NPCSprite>();
    }

    void Start()
    {
        foreach (var other in FindObjectsByType<NPCMeeting>(FindObjectsSortMode.None))
            if (other != this && body != null && other.body != null)
                Physics2D.IgnoreCollision(body, other.body);
    }

    void OnTriggerEnter2D(Collider2D other)
    {
        // 遠端接管時不自發寒暄——「被人搭話」屬於後端決策點
        // ponytail: 接真大腦時改成回報 {type:"encounter"} 事件讓後端決定
        if (BrainGateway.RemoteMode) return;
        if (other.isTrigger) return; // 只認對方的實體圓，避免一對重複觸發
        var peer = other.GetComponent<NPCMeeting>();
        if (peer == null || peer == this) return;
        if (Busy || peer.Busy) return;
        if (Time.time < cooldownUntil || Time.time < peer.cooldownUntil) return;
        if (sprite.Action != null || peer.sprite.Action != null) return; // 坐著的不搭話
        if (string.CompareOrdinal(name, peer.name) > 0) return; // 名字序小的主持，避免雙發
        StartCoroutine(Meet(peer));
    }

    IEnumerator Meet(NPCMeeting peer)
    {
        Busy = true;
        peer.Busy = true;
        mover.Stop();
        peer.mover.Stop();
        var dir = (Vector2)(peer.transform.position - transform.position);
        mover.Face(dir);
        peer.mover.Face(-dir);
        bubble.enabled = true;
        peer.bubble.enabled = true;
        yield return new WaitForSeconds(Random.Range(2.5f, 4.5f));
        bubble.enabled = false;
        peer.bubble.enabled = false;
        cooldownUntil = peer.cooldownUntil = Time.time + 25f; // 冷卻，避免原地重複寒暄
        Busy = false;
        peer.Busy = false;
    }

    public IEnumerator ShowBubble(float seconds)
    {
        bubble.enabled = true;
        yield return new WaitForSeconds(seconds);
        bubble.enabled = false;
    }
}

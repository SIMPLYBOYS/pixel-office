using UnityEngine;

// 公司大門的自動門：有人靠近就開、走掉就關。
//
// 【純呈現，不擋路】碰撞圖上那兩格本來就是通的（West 裡標 'D'，合成 Collision 時併成 '.'）。
// 不做成會擋路的門，是因為那要引入「暫時不可走」：尋路得重算、剛好走到門口的人會真的卡住，
// 而畫面上只看得出「他站著不動」——跟閒置一模一樣，又多一種沒人會發現的失敗模式。
// 門開不開只是「現在有沒有人進出」的投影，這件事本身就是真的，不必靠擋路來證明。
public class AutoDoor : MonoBehaviour
{
    public Sprite[] frames;            // 0＝關，最後一張＝全開（來源只取前半，後半是鏡像）
    public float range = 2.2f;         // 幾個世界單位內算「有人靠近」（≈2 格）
    public float speed = 8f;           // 每秒播幾張

    // 門的 pivot 在左下，但感應要用【門中心】算距離，否則站在門右側的人會比左側的人晚觸發。
    static readonly Vector3 Center = new(1f, 1f, 0f);

    SpriteRenderer sr;
    Transform[] npcs;
    float t;                            // 0..1 開合進度

    void Start()
    {
        sr = GetComponent<SpriteRenderer>();
        // NPC 在進 Play 之前就由 CharacterBuilder 建好了，抓一次就夠；每幀 Find 是白費。
        var movers = Object.FindObjectsByType<NPCMover>(FindObjectsSortMode.None);
        npcs = new Transform[movers.Length];
        for (int i = 0; i < movers.Length; i++) npcs[i] = movers[i].transform;
    }

    void Update()
    {
        if (frames == null || frames.Length < 2 || sr == null) return;

        var c = transform.position + Center;
        bool near = false;
        foreach (var n in npcs)
            if (n != null && (n.position - c).sqrMagnitude < range * range) { near = true; break; }

        t = Mathf.MoveTowards(t, near ? 1f : 0f, speed / (frames.Length - 1) * Time.deltaTime);
        sr.sprite = frames[Mathf.Clamp(Mathf.RoundToInt(t * (frames.Length - 1)), 0, frames.Length - 1)];
    }
}

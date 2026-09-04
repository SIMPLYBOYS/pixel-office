using UnityEngine;

// ponytail: 不用 AnimatorController——sprite 陣列 + 時間取幀，零資產零狀態圖
public class NPCSprite : MonoBehaviour
{
    public Sprite[] idleDown, idleUp, idleRight, idleLeft;
    public Sprite[] walkDown, walkUp, walkRight, walkLeft;
    public Sprite[] sitRight, sitLeft;
    public Sprite[] phone, sleep;   // 等外部回應／長時間沒事做——都是「站著不動」看不出來的狀態
    public Sprite[] book;           // 低頭看書：連續讀檔/查資料（素材只有正面單向）
    public Sprite[] giftUp, giftDown, giftRight, giftLeft;  // 遞交成果（委派收件成功的交付戲）
    // 一次性動作（受傷／撿起／舉起／丟出，各四向）：疊在目前姿勢上演一輪就自己退掉，
    // 橋不用算時間、不用記「他剛才是什麼姿勢」再還原。名稱＝切幀的檔名段（hurt_up…）。
    [System.Serializable] public class Clip { public string name; public Sprite[] frames; }
    public Clip[] clips;
    public float fps = 8f;

    SpriteRenderer sr;
    NPCMover mover;
    string action; // null / "sit_up" / "sit_left" / "sit_right"
    Sprite[] once; float onceAt;   // 正在播的一次性動作

    void Awake()
    {
        sr = GetComponent<SpriteRenderer>();
        mover = GetComponent<NPCMover>();
    }

    public string Action => action;

    // 坐下。排序不做任何特殊處理——物件粒度（椅子獨立 sprite）讓純 Y-sort 天然正確：
    // 坐著的人在桌帶前、椅背後，上身自然可見
    public void SetAction(string a)
    {
        var clip = a == null ? null : System.Array.Find(clips ?? new Clip[0], c => c.name == a);
        if (clip != null && clip.frames.Length > 0) { once = clip.frames; onceAt = Time.time; return; }
        action = a;
        if (a == null) once = null;   // 走位會清姿勢，一次性動作也一起清（半路上閃紅更怪）
    }

    void Update()
    {
        if (once != null)
        {
            int i = (int)((Time.time - onceAt) * fps);
            // 至少演一輪、至少一秒：受傷只有 3 幀，一輪 0.4 秒眨眼就過；舉起 14 幀一輪就夠
            if (i >= Mathf.Max(once.Length, Mathf.CeilToInt(fps))) once = null;
            else if (!mover.Moving) { sr.sprite = once[i % once.Length]; return; }
        }
        var set = Pick();
        if (set == null || set.Length == 0) return;
        sr.sprite = set[(int)(Time.time * fps) % set.Length];
    }

    Sprite[] Pick()
    {
        if (!mover.Moving && action != null)
            return action switch
            {
                "sit_left" => sitLeft,
                "sit_right" => sitRight,
                "phone" => phone,
                "sleep" => sleep,
                "book" => book,
                "gift_up" => giftUp,
                "gift_down" => giftDown,
                "gift_right" => giftRight,
                "gift_left" => giftLeft,
                // 轉向：站著不動、只換朝向（走到同事桌邊要面對人，站白板前要面對板子）
                "face_left" => idleLeft,
                "face_right" => idleRight,
                "face_up" => idleUp,
                "face_down" => idleDown,
                _ => idleUp, // sit_up：背對鏡頭
            };
        var f = mover.Facing;
        if (mover.Moving)
            return f == Vector2.up ? walkUp
                 : f == Vector2.right ? walkRight
                 : f == Vector2.left ? walkLeft : walkDown;
        return f == Vector2.up ? idleUp
             : f == Vector2.right ? idleRight
             : f == Vector2.left ? idleLeft : idleDown;
    }
}

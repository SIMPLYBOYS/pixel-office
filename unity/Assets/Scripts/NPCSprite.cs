using UnityEngine;

// ponytail: 不用 AnimatorController——sprite 陣列 + 時間取幀，零資產零狀態圖
public class NPCSprite : MonoBehaviour
{
    public Sprite[] idleDown, idleUp, idleRight, idleLeft;
    public Sprite[] walkDown, walkUp, walkRight, walkLeft;
    public Sprite[] sitRight, sitLeft;
    public Sprite[] phone, sleep;   // 等外部回應／長時間沒事做——都是「站著不動」看不出來的狀態
    public float fps = 8f;

    SpriteRenderer sr;
    NPCMover mover;
    string action; // null / "sit_up" / "sit_left" / "sit_right"

    void Awake()
    {
        sr = GetComponent<SpriteRenderer>();
        mover = GetComponent<NPCMover>();
    }

    public string Action => action;

    // 坐下。排序不做任何特殊處理——物件粒度（椅子獨立 sprite）讓純 Y-sort 天然正確：
    // 坐著的人在桌帶前、椅背後，上身自然可見
    public void SetAction(string a) => action = a;

    void Update()
    {
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

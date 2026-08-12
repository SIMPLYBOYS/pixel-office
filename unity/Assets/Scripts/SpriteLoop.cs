using UnityEngine;

// 定點循環播放一組幀。給「不是 agent 投影」的常駐角色用（總機小姐）——
// 她不掛 NPCSprite/NPCMover 那一整套：那套的每個狀態都對應真實 agent 狀態
// （走位、入座、講電話），而她沒有任何狀態可投影，只有待機呼吸。
// 哪天「訪客上門」成為真實事件，再把她升級成完整 NPC——素材已經是全套 72 幀。
public class SpriteLoop : MonoBehaviour
{
    public Sprite[] frames;
    public float fps = 6f;
    SpriteRenderer sr;

    void Awake() { sr = GetComponent<SpriteRenderer>(); }

    void Update()
    {
        if (frames != null && frames.Length > 0)
            sr.sprite = frames[(int)(Time.time * fps) % frames.Length];
    }
}

using UnityEngine;

// 頭邊的狀態徽章：掛上去就一直在，直到被明確收掉。
//
// 跟 NPCSpeech 的泡泡【刻意分工】：泡泡是轉瞬的（2.5-7 秒自己消失），講「剛剛發生了什麼」；
// 徽章是持續的，講「他【現在】卡在什麼狀態」。差別在於掃一眼辦公室就看得出誰動不了——
// 泡泡給不了這個，因為你多半沒在看的那幾秒它就過去了。
//
// ponytail: 沿用 NPCSprite 那套（sprite 陣列 + 時間取幀），不引入 Animator
public class NPCEmote : MonoBehaviour
{
    [System.Serializable]
    public class Set
    {
        public string name;
        public Sprite[] frames;   // 兩幀微動畫（LimeZu 的圖示本來就是成對的）
    }

    public Set[] sets;
    public float fps = 3f;        // 徽章是背景資訊，跳太快會搶掉主畫面的注意力

    SpriteRenderer sr;
    Sprite[] cur;

    void Awake()
    {
        sr = GetComponent<SpriteRenderer>();
        sr.enabled = false;
    }

    /// 空字串＝收起來。認不得的名字【也收起來】而不是留著上一個——
    /// 留著就變成「狀態早就過了、徽章還掛在頭上」，那是假投影。
    public void Show(string name)
    {
        cur = null;
        if (!string.IsNullOrEmpty(name) && sets != null)
            foreach (var s in sets)
                if (s.name == name && s.frames != null && s.frames.Length > 0)
                {
                    cur = s.frames;
                    break;
                }
        if (cur == null && !string.IsNullOrEmpty(name))
            Debug.LogWarning($"NPCEmote: 沒有 '{name}' 這個徽章，先收起來");
        sr.enabled = cur != null;
    }

    void Update()
    {
        if (cur == null) return;
        sr.sprite = cur[(int)(Time.time * fps) % cur.Length];
    }
}

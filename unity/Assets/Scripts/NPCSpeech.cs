using UnityEngine;

// 頭上文字對話框：TextMesh（動態字型，中文原生支援）+ 9-slice 白底自動縮放
// 顯示時間依字數自動調整；「...」寒暄泡泡仍由 NPCMeeting 負責
public class NPCSpeech : MonoBehaviour
{
    public TextMesh text;
    public SpriteRenderer panel;

    float hideAt = -1f;

    public void Show(string content)
    {
        if (string.IsNullOrEmpty(content) || text == null) return;
        text.text = Wrap(content, 8);
        float seconds = Mathf.Clamp(1.5f + content.Length * 0.25f, 2.5f, 7f);

        text.gameObject.SetActive(true);
        var size = text.GetComponent<MeshRenderer>().bounds.size;
        panel.size = new Vector2(size.x + 0.5f, size.y + 0.35f);
        panel.gameObject.SetActive(true);
        hideAt = Time.time + seconds;
    }

    void Update()
    {
        if (hideAt > 0f && Time.time >= hideAt) Hide();
    }

    public void Hide()
    {
        if (text != null) text.gameObject.SetActive(false);
        if (panel != null) panel.gameObject.SetActive(false);
        hideAt = -1f;
    }

    // 中文無空格斷詞，固定字數硬換行
    static string Wrap(string s, int perLine)
    {
        if (s.Length <= perLine) return s;
        var sb = new System.Text.StringBuilder();
        for (int i = 0; i < s.Length; i += perLine)
        {
            if (i > 0) sb.Append('\n');
            sb.Append(s.Substring(i, Mathf.Min(perLine, s.Length - i)));
        }
        return sb.ToString();
    }
}

using System.Collections.Generic;
using UnityEngine;

// 16×17 碰撞 ASCII 的 BFS 格子尋路（4 方向）。RoomBuilder 建房時把地圖塞進來
// ponytail: 地圖就 272 格，BFS 每次全跑也毫無感覺；要大地圖再上 A*
public class NavGrid : MonoBehaviour
{
    public string[] rows;

    bool Free(int c, int r) =>
        r >= 0 && r < rows.Length && c >= 0 && c < rows[r].Length && rows[r][c] == '.';

    static Vector2 Center(int c, int r) => new(c + 0.5f, -(r + 0.5f));
    static (int c, int r) Cell(Vector2 p) => (Mathf.FloorToInt(p.x), Mathf.FloorToInt(-p.y));

    (int c, int r)? Nearest((int c, int r) cell)
    {
        if (Free(cell.c, cell.r)) return cell;
        for (int rad = 1; rad <= 3; rad++)
            for (int dr = -rad; dr <= rad; dr++)
                for (int dc = -rad; dc <= rad; dc++)
                    if (Free(cell.c + dc, cell.r + dr)) return (cell.c + dc, cell.r + dr);
        return null;
    }

    static readonly (int dc, int dr)[] Steps = { (1, 0), (-1, 0), (0, 1), (0, -1) };

    // Scene 視窗紅格 = 碰撞圖疊在美術上，twin 錯位一眼現形（Game 視窗開 Gizmos 也看得到）
    void OnDrawGizmos()
    {
        if (rows == null) return;
        Gizmos.color = new Color(1f, 0f, 0f, 0.25f);
        for (int r = 0; r < rows.Length; r++)
            for (int c = 0; c < rows[r].Length; c++)
                if (rows[r][c] == '#')
                    Gizmos.DrawCube(new Vector3(c + 0.5f, -(r + 0.5f), 0), Vector3.one);
    }

    public List<Vector2> FindPath(Vector2 from, Vector2 to)
    {
        var path = new List<Vector2>();
        var s = Nearest(Cell(from));
        var g = Nearest(Cell(to));
        if (s == null || g == null)
        {
            Debug.LogWarning($"NavGrid: {from}→{to} 起訖點附近無可走格，取消移動");
            return path; // 空路徑 = 原地不動，絕不硬走穿牆
        }

        var prev = new Dictionary<(int, int), (int, int)>();
        var q = new Queue<(int, int)>();
        q.Enqueue(s.Value);
        prev[s.Value] = s.Value;
        while (q.Count > 0)
        {
            var cur = q.Dequeue();
            if (cur == g.Value) break;
            foreach (var (dc, dr) in Steps)
            {
                var nxt = (cur.Item1 + dc, cur.Item2 + dr);
                if (Free(nxt.Item1, nxt.Item2) && !prev.ContainsKey(nxt))
                {
                    prev[nxt] = cur;
                    q.Enqueue(nxt);
                }
            }
        }
        if (!prev.ContainsKey(g.Value))
        {
            Debug.LogWarning($"NavGrid: {from}→{to} 無可達路徑（碰撞圖孤島？），取消移動");
            return path;
        }

        var back = g.Value;
        while (back != prev[back])
        {
            path.Add(Center(back.Item1, back.Item2));
            back = prev[back];
        }
        path.Reverse();
        path.Add(to); // 最後貼到精確目標點
        return path;
    }
}

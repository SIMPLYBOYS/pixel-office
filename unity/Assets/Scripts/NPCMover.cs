using System.Collections.Generic;
using UnityEngine;

// 沿 NavGrid 路徑走。Dynamic RB = 物理硬保證不穿透；
// 不卡牆靠：碰撞半徑 0.18（格心到牆間隙 0.25）+ 零摩擦材質
public class NPCMover : MonoBehaviour
{
    public float speed = 2f;

    public Vector2 Facing { get; private set; } = Vector2.down;
    public bool Moving { get; private set; }
    public bool Arrived => path.Count == 0;

    Rigidbody2D rb;
    NavGrid nav;
    readonly Queue<Vector2> path = new();

    void Awake()
    {
        rb = GetComponent<Rigidbody2D>();
        nav = FindFirstObjectByType<NavGrid>();
    }

    public void MoveTo(Vector2 pos)
    {
        path.Clear();
        if (nav == null)
        {
            path.Enqueue(pos);
            return;
        }
        var pts = nav.FindPath(rb.position, pos);
        foreach (var p in pts) path.Enqueue(p);
        // 路徑只走到格心。目標不在格心（總機崗位對準櫃檯上的洞，見 RoomBuilder.Nudge）又【就在終點那一格裡】才補最後一小段；
        // 不在同一格（目標落在牆裡、Nearest 挑了旁邊那格、或根本沒路）就不補——絕不硬走穿牆
        var end = pts.Count > 0 ? pts[^1] : rb.position;
        if (NavGrid.SameCell(end, pos) && (pos - end).sqrMagnitude > 0.0001f) path.Enqueue(pos);
    }

    public void Stop() => path.Clear();

    public void Face(Vector2 dir) =>
        Facing = Mathf.Abs(dir.x) >= Mathf.Abs(dir.y)
            ? (dir.x > 0 ? Vector2.right : Vector2.left)
            : (dir.y > 0 ? Vector2.up : Vector2.down);

    void FixedUpdate()
    {
        if (path.Count == 0)
        {
            Moving = false;
            rb.linearVelocity = Vector2.zero;
            return;
        }
        var target = path.Peek();
        var to = target - rb.position;
        if (to.magnitude < 0.08f)
        {
            path.Dequeue();
            if (path.Count == 0)
            {
                rb.linearVelocity = Vector2.zero;
                Moving = false;
            }
            return;
        }
        var dir = to.normalized;
        rb.linearVelocity = dir * speed;
        Moving = true;
        Facing = Mathf.Abs(dir.x) >= Mathf.Abs(dir.y)
            ? (dir.x > 0 ? Vector2.right : Vector2.left)
            : (dir.y > 0 ? Vector2.up : Vector2.down);
    }
}

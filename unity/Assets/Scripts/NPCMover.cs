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
        if (nav != null)
            foreach (var p in nav.FindPath(rb.position, pos)) path.Enqueue(p);
        else
            path.Enqueue(pos);
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

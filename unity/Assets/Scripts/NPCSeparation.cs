using System.Collections.Generic;
using UnityEngine;

// 站定時把互相重疊的 NPC 輕輕推開（走路交給尋路、坐著的不動，只推站著的）
public class NPCSeparation : MonoBehaviour
{
    const float Radius = 0.45f;
    const float Strength = 2f;

    static readonly List<NPCSeparation> all = new();

    Rigidbody2D rb;
    NPCMover mover;
    NPCSprite sprite;

    void Awake()
    {
        rb = GetComponent<Rigidbody2D>();
        mover = GetComponent<NPCMover>();
        sprite = GetComponent<NPCSprite>();
    }

    void OnEnable() => all.Add(this);
    void OnDisable() => all.Remove(this);

    void FixedUpdate()
    {
        if (mover.Moving || sprite.Action != null) return; // 移動中或坐著不推
        var push = Vector2.zero;
        foreach (var other in all)
        {
            if (other == this) continue;
            var d = rb.position - other.rb.position;
            float dist = d.magnitude;
            if (dist < Radius)
                push += (dist < 0.01f ? Random.insideUnitCircle.normalized : d / dist) * (Radius - dist);
        }
        if (push != Vector2.zero)
            rb.MovePosition(rb.position + push * (Strength * Time.fixedDeltaTime));
    }
}

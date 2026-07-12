using System.Collections.Generic;
using System.Linq;
using UnityEngine;

// 場景 Waypoints 底下的具名互動點註冊表（名字格式：key@action，action 可省略）
// Claim/Release 避免兩個 NPC 坐同一張椅子
public static class WaypointRegistry
{
    public class Entry
    {
        public string key;      // 「chair_1」
        public string action;   // 「sit_up」或 null（站立點）
        public Transform pos;
        public object claimedBy;
    }

    static List<Entry> entries;

    static void Init()
    {
        if (entries != null && entries.Count > 0 && entries[0].pos != null) return;
        entries = new List<Entry>();
        var root = GameObject.Find("Waypoints");
        if (root == null) return;
        foreach (Transform t in root.transform)
        {
            var parts = t.name.Split('@');
            entries.Add(new Entry
            {
                key = parts[0],
                action = parts.Length > 1 ? parts[1] : null,
                pos = t,
            });
        }
    }

    public static Entry Get(string key)
    {
        Init();
        return entries.FirstOrDefault(e => e.key == key);
    }

    public static List<Entry> All()
    {
        Init();
        return entries;
    }

    public static Entry ClaimRandom(object who)
    {
        Init();
        var free = entries.Where(e => e.claimedBy == null).ToList();
        if (free.Count == 0) return null;
        var e = free[Random.Range(0, free.Count)];
        e.claimedBy = who;
        return e;
    }

    public static void Release(object who)
    {
        if (entries == null) return;
        foreach (var e in entries)
            if (e.claimedBy == who) e.claimedBy = null;
    }
}

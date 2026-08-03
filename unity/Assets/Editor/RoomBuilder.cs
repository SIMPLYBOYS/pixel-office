using System.Collections.Generic;
using System.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.Tilemaps;

// LimeZu 官方 Office_Design_2 拆層組裝：
//   Background = 地板+牆一張底圖（永遠墊底，不參與 Y-sort）
//   Props      = 家具連通元件 sprites（pivot 左下 + sortPoint Pivot → 與角色正確遮擋）
//   Collision  = 手寫 ASCII 對照設計圖（16×17 cells，之後角色實走再校）
//   Waypoints  = 手標座標
// 座標系：設計圖左上 = 世界 (0,0)，往下為 -y；像素 ÷16 = 世界單位
// ponytail: 螢幕/飲水機動畫層（aseprite L2/L3 六幀）先烙 frame0 靜態，要動效再切 AnimationClip
public static class RoomBuilder
{
    const string SpriteRoot = "Assets/Sprites";
    const string TileFolder = "Assets/Tiles";
    const string FurnitureJson = "Assets/Sprites/LimeZu/Design/furniture.json";
    const float PPU = 16f;

    // 家具位置唯一真相 = tools/extract_design.py 的輸出 furniture.json，不手抄
    [System.Serializable]
    class Item { public string name; public int x, y, w, h; }
    [System.Serializable]
    class FurnitureData { public int canvasW, artH; public Item[] items; }

    // '#'=擋 '.'=通
    static readonly string[] Collision =
    {
        "################",
        "################",
        "#.#............#",
        "#..#########.###",
        "#..#########.###",
        "#..............#",
        "#..............#",
        "#.############.#",   // x13＝下排長桌(obj_24)東端桌面，原本漏封→NPC 直接穿桌
        "#.############.#",   // x14 是桌東側走道，實心像素為 0，維持可走
        "#.............##",
        "##########.#####",
        "##########.#####",
        "#######.#......#",   // 老闆房：桌子北側整排是地板（原本誤封）
        "######..##..##.#",   // x12,13＝桌面；x14＝桌東側走道
        "######.###..#..#",   // x13＝老闆椅座位（可入座）、x14＝走道
        "######......####",
        "################",
    };

    // (名稱, cell x, cell y)，cell 以設計圖左上為 (0,0)
    // 「@動作」後綴：NPC 到點後執行（sit_up=背對鏡頭入座、sit_left/right=側面坐姿）
    static readonly (string name, int cx, int cy)[] Waypoints =
    {
        ("chair_1@sit_up", 4, 5), ("chair_2@sit_up", 7, 5), ("chair_3@sit_up", 10, 5), // 上排座位
        ("chair_4@sit_up", 3, 9), ("chair_5@sit_up", 6, 9),
        ("chair_6@sit_up", 9, 9), ("chair_7@sit_up", 12, 9),  // 下排南側座位（對齊 obj_24~27 的 x=48/96/144/192）
        ("chair_8@sit_right", 7, 12), // 左下房橘椅（椅背在西，坐姿面東）
        ("boss_1", 11, 14),           // 老闆房西側走道（等審批時站這裡罰站）
        ("boss_seat@sit_left", 13, 14),  // 老闆桌對面的那張椅子（椅背在東→坐姿面西對螢幕）
        ("cooler_1", 9, 12),          // 飲水機前（站著）
        ("board_1@face_up", 10, 2),   // 折線圖白板前（obj_03 在 (10,0)），背對鏡頭＝面向板子
        // 各工位旁的站位：委派時主 agent 走過來，面向坐著的同事
        ("side_1@face_up", 4, 6), ("side_2@face_up", 7, 6), ("side_3@face_up", 10, 6),
        ("side_4@face_right", 2, 9), ("side_5@face_right", 5, 9),
        ("side_6@face_right", 8, 9), ("side_7@face_right", 11, 9),
        ("printer_1", 13, 5),         // 印表機前（站著）
    };

    [MenuItem("Tools/Build Room")]
    public static void Build()
    {
        AssetDatabase.ImportAsset(SpriteRoot,
            ImportAssetOptions.ImportRecursive | ImportAssetOptions.ForceUpdate);

        if (Collision.Length != 17 || Collision.Any(r => r.Length != 16))
        {
            Debug.LogError("RoomBuilder: 碰撞地圖不是 16×17");
            return;
        }

        var jsonAsset = AssetDatabase.LoadAssetAtPath<TextAsset>(FurnitureJson);
        if (jsonAsset == null)
        {
            Debug.LogError($"RoomBuilder: 找不到 {FurnitureJson}，先跑 tools/extract_design.py 並複製產物");
            return;
        }
        var data = JsonUtility.FromJson<FurnitureData>(jsonAsset.text);
        float artH = data.artH;

        // ⚠ 先把要用到的 sprite 全部解析出來，解析不到就【整批放棄、不動現有場景】。
        // 為什麼：下面第一件事是 DestroyImmediate 舊房間，而 ForceUpdate 的遞迴重匯入若還沒跑完
        // （剛加進 180 張新角色圖那次就是），FindAssets 會查無資產 → 房間被砍掉、新物件的 sprite
        // 全是 null ＝ 整間辦公室變透明。先驗後拆，最壞只是「這次沒建成」。
        var need = new List<string> { "bg_base", "lz_wall" };
        need.AddRange(data.items.Select(it => it.name));
        var sprites = new Dictionary<string, Sprite>();
        foreach (var n in need.Distinct())
        {
            var sp = FindSprite(n);
            if (sp == null)
            {
                Debug.LogError($"RoomBuilder: sprite「{n}」尚未匯入完成——等 Unity 匯入跑完再按一次 " +
                               "Tools/Build Room。本次【未改動場景】。");
                return;
            }
            sprites[n] = sp;
        }

        SetYSort();

        foreach (var name in new[] { "Room", "Waypoints" })
        {
            var old = GameObject.Find(name);
            if (old != null) Object.DestroyImmediate(old);
        }

        var room = new GameObject("Room");
        room.AddComponent<NavGrid>().rows = Collision; // NPC 尋路用同一張碰撞圖

        // 底圖
        var bg = new GameObject("Background");
        bg.transform.SetParent(room.transform, false);
        bg.transform.localPosition = new Vector3(0, -artH / PPU, 0);
        var bgSr = bg.AddComponent<SpriteRenderer>();
        bgSr.sprite = sprites["bg_base"];
        bgSr.sortingOrder = -20;

        // 家具（每件獨立物件，位置直接來自 json）
        var props = new GameObject("Props");
        props.transform.SetParent(room.transform, false);
        foreach (var it in data.items)
        {
            var go = new GameObject(it.name);
            go.transform.SetParent(props.transform, false);
            go.transform.localPosition = new Vector3(it.x / PPU, -(it.y + it.h) / PPU, 0);
            var sr = go.AddComponent<SpriteRenderer>();
            sr.sprite = sprites[it.name];
            sr.sortingOrder = 0;
            sr.spriteSortPoint = SpriteSortPoint.Pivot; // pivot=左下 → 以底邊 Y-sort
        }

        // 碰撞（無 renderer 的 tilemap）
        var gridGo = new GameObject("CollisionGrid");
        gridGo.transform.SetParent(room.transform, false);
        gridGo.AddComponent<Grid>();
        var colGo = new GameObject("Collision");
        colGo.transform.SetParent(gridGo.transform, false);
        var tm = colGo.AddComponent<Tilemap>();
        tm.tileAnchor = Vector3.zero;
        var col = colGo.AddComponent<TilemapCollider2D>();
        col.compositeOperation = Collider2D.CompositeOperation.Merge;
        colGo.AddComponent<CompositeCollider2D>();
        colGo.GetComponent<Rigidbody2D>().bodyType = RigidbodyType2D.Static;
        var wallTile = GetTile("lz_wall", true);   // sprite 已在上面驗過
        for (int r = 0; r < Collision.Length; r++)
            for (int c = 0; c < 16; c++)
                if (Collision[r][c] == '#')
                    tm.SetTile(new Vector3Int(c, -(r + 1), 0), wallTile);

        // Waypoints
        var wps = new GameObject("Waypoints");
        foreach (var (name, cx, cy) in Waypoints)
        {
            var wp = new GameObject(name);
            wp.transform.SetParent(wps.transform);
            wp.transform.position = new Vector3(cx + 0.5f, -(cy + 0.5f), 0);
        }

        ValidateWaypoints();

        // 相機對準房間中心
        var cam = Camera.main;
        if (cam != null) cam.transform.position = new Vector3(8f, -artH / PPU / 2f, -10f);

        UnityEditor.SceneManagement.EditorSceneManager.MarkSceneDirty(
            UnityEngine.SceneManagement.SceneManager.GetActiveScene());
        Debug.Log($"RoomBuilder: 設計圖房間完成——{data.items.Length} 件家具、{Waypoints.Length} 個 waypoint");
    }

    // 每次 Build 驗證：所有 waypoint 都落在可走格，且從出生點 BFS 可達（孤島直接報錯）
    static void ValidateWaypoints()
    {
        var seen = new HashSet<(int, int)> { (8, 5) };
        var q = new Queue<(int, int)>();
        q.Enqueue((8, 5));
        while (q.Count > 0)
        {
            var (c, r) = q.Dequeue();
            foreach (var (dc, dr) in new[] { (1, 0), (-1, 0), (0, 1), (0, -1) })
            {
                var n = (c + dc, r + dr);
                if (!seen.Contains(n) && n.Item2 >= 0 && n.Item2 < Collision.Length
                    && n.Item1 >= 0 && n.Item1 < 16 && Collision[n.Item2][n.Item1] == '.')
                {
                    seen.Add(n);
                    q.Enqueue(n);
                }
            }
        }
        foreach (var (name, cx, cy) in Waypoints)
            if (Collision[cy][cx] != '.' || !seen.Contains((cx, cy)))
                Debug.LogError($"RoomBuilder: waypoint {name} ({cx},{cy}) 不可達或在牆裡！");
    }

    // URP 2D 的 Y-sort 在 Renderer2DData 上
    static void SetYSort()
    {
        foreach (var guid in AssetDatabase.FindAssets("t:Renderer2DData"))
        {
            var asset = AssetDatabase.LoadAssetAtPath<ScriptableObject>(AssetDatabase.GUIDToAssetPath(guid));
            var so = new SerializedObject(asset);
            var mode = so.FindProperty("m_TransparencySortMode");
            var axis = so.FindProperty("m_TransparencySortAxis");
            if (mode == null || axis == null) continue;
            mode.intValue = (int)TransparencySortMode.CustomAxis;
            axis.vector3Value = Vector3.up;
            so.ApplyModifiedPropertiesWithoutUndo();
        }
        AssetDatabase.SaveAssets();
    }

    static Tile GetTile(string spriteName, bool collide)
    {
        if (!AssetDatabase.IsValidFolder(TileFolder)) AssetDatabase.CreateFolder("Assets", "Tiles");
        string path = $"{TileFolder}/{spriteName}{(collide ? "_col" : "")}.asset";
        var tile = AssetDatabase.LoadAssetAtPath<Tile>(path);
        if (tile == null)
        {
            tile = ScriptableObject.CreateInstance<Tile>();
            AssetDatabase.CreateAsset(tile, path);
        }
        tile.sprite = FindSprite(spriteName);
        tile.colliderType = collide ? Tile.ColliderType.Grid : Tile.ColliderType.None;
        EditorUtility.SetDirty(tile);
        return tile;
    }

    static Sprite FindSprite(string name)
    {
        foreach (var guid in AssetDatabase.FindAssets($"t:Sprite {name}", new[] { SpriteRoot }))
            foreach (var s in AssetDatabase.LoadAllAssetsAtPath(AssetDatabase.GUIDToAssetPath(guid)).OfType<Sprite>())
                if (s.name == name) return s;
        Debug.LogError($"RoomBuilder: 找不到 sprite '{name}'");
        return null;
    }
}

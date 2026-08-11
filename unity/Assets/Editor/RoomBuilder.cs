using System.Collections.Generic;
using System.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.Tilemaps;

// LimeZu 官方 Office_Design_2 拆層組裝：
//   Background = 地板+牆一張底圖（永遠墊底，不參與 Y-sort）
//   Props      = 家具連通元件 sprites（pivot 左下 + sortPoint Pivot → 與角色正確遮擋）
//   Collision  = 手寫 ASCII，西側新區(10 寬) ⊕ 辦公區(16 寬，對照設計圖) = 26×17 cells
//   Waypoints  = 手標座標
// 座標系：【地圖】左上 = 世界 (0,0)，往下為 -y；像素 ÷16 = 世界單位。
// 設計圖座標（furniture.json、OfficeWaypoints）要加 OfficeX 才是世界座標。
// ponytail: 螢幕/飲水機動畫層（aseprite L2/L3 六幀）先烙 frame0 靜態，要動效再切 AnimationClip
public static class RoomBuilder
{
    const string SpriteRoot = "Assets/Sprites";
    const string TileFolder = "Assets/Tiles";
    const string FurnitureJson = "Assets/Sprites/LimeZu/Design/furniture.json";

    // 西側新區的美術（tools/compose_room.py 拼出來的，schema 跟 furniture.json 完全一樣，
    // 所以下面載入與擺放的程式碼兩區共用一份）。缺檔不算錯——只是西區還沒拼，房間照建。
    const string WestJson = "Assets/Sprites/LimeZu/Design/west.json";
    const float PPU = 16f;

    // 刻意不擺的家具：只有右下角那盆植物。
    // 第 6~8 列封起來之後，上下半的跨區通道剩最左那行 x=1，移動看起來很制式；而右側
    // x=14 明明整條是走道，唯一的塞子就是 (14,9) 這盆——它底下的 bg_base 本來就是地板。
    // 拔掉它右側南北通道立刻打通，四組工作站一組都不用犧牲。
    // ⚠ 它的葉尖碰到長桌、被 extract_design 併進 obj_24，藏 obj_29 只清得掉盆身——
    //   剩下那撮葉子由 tools/clean_props.py 擦掉（素材不進 git，重新抽圖後要再跑一次）。
    static readonly HashSet<string> Hidden = new() { "obj_29" };

    // 家具位置唯一真相 = tools/extract_design.py 的輸出 furniture.json，不手抄
    [System.Serializable]
    class Item { public string name; public int x, y, w, h; }
    [System.Serializable]
    class FurnitureData { public int canvasW, artH; public Item[] items; }

    // OfficeX 是原辦公區在新地圖上的左緣。玄關與會議室往【西】擴，所以辦公區整體東移這麼多格。
    //
    // 抽成常數而不是把座標抄一遍：底圖、家具、waypoint 三處都要位移，寫死就是三份要同步的
    // 魔術數字。furniture.json 是家具位置的唯一真相（設計圖座標），位移只在【擺放的那一刻】
    // 加一次，json 不動——重抽圖也不必重算。
    public const int OfficeX = 13;   // CharacterBuilder 的出生點也靠它位移

    // 西側新區（10 寬）。三種字元：
    //   '.'  可走
    //   'D'  自動門所在格——判定上同 '.'（門不擋路，只是投影「有人進出」）
    //   'T'  家具擋路——判定上同 '#'，但【美術上是地板】。分開是因為 compose_room.py 照這張圖
    //        畫底圖，把桌子畫成牆的話，桌子底下會出現一道有白色頂面和陰影的牆。
    //
    // 西翼＝【會議室在上、公司大門在下、門外是公共區】。這個結構取自 LimeZu 官方
    // Office_Design_1：它下半那塊淺色大廳其實在辦公室【外面】，販賣機與等候椅都在門外，
    // 公司的門開在中間那道牆上。家具直接借那張圖拆出來的元件（d1_* 前綴）。
    //
    //     0123456789012
    //  2  #TTTT.#.TT..#   櫃檯（左）｜ 會議室（右），中間【垂直】牆一欄
    //  5  #............   ← 門內走廊，直接接辦公區
    //  7  ######DD#####   ← 公司南牆＋自動門，【橫向】牆兩列（牆的美術本來就 32px 高）
    // 14  #T.TT.TTTT..#   門外靠南牆一排：販賣機／飲水機／影印機／書架／盆栽

    static readonly string[] West =
    {
        "#############",
        "#############",   // 北外牆
        "#TTTT.#.TT..#",   // 左：櫃檯（訪客站 x5）｜ 右：會議室（長桌 x8-9）
        "#TTTT.#.TT..#",   //   兩間之間是【垂直】牆，一欄就夠——垂直牆本來就是一欄寬
        "#TTTT.#.TT..#",
        "#............",   // ← 門內走廊，(12,5) 接辦公區
        "#...........#",
        "######DD#####",   // ← 公司南牆＋自動門。【橫向】牆一律兩列：LimeZu 的牆是
        "######DD#####",   //   「6px 頂面 + 26px 牆面」共 32px＝2 格，做成一格的話牆比
                           //   掛畫還矮（畫 20px），東西掛上去會浮在牆外面。門也剛好
                           //   2 格寬 × 2 格高，對上 animated_door_sliding_glass 的尺寸
        "#...........#",   // 門外大廳（掛畫掛在上面那道牆的牆面上）
        "#.TT.TT.....#",   //   圓椅、盆栽＋藍椅
        "#...........#",
        "#.TT.TT.....#",   //   藍色候客椅、盆栽
        "#...........#",
        "#T.TT.TTTT..#",   //   靠南牆一排：盆栽、販賣機、飲水機、影印機、書架
        "#############",   // 南外牆（同樣兩列，這排東西才有牆可以靠）
        "#############",
    };

    // '#'=擋 '.'=通。這 16 欄是【辦公區本身】的座標系（設計圖左上＝(0,0)），註解裡的
    // 欄號都是這個系統——刻意不把 West 併進來重寫，那些「x13＝長桌東端」的註記才對得回設計圖。
    static readonly string[] OfficeRows =
    {
        "################",
        "################",
        "#.##########...#",   // 上排隔間 obj_04 佔到第 2 列（覆蓋 43~77%），原本整排漏封
        "#..#########.###",
        "#..#########.###",
        "...............#",   // 中央走道＝第 5 列（上排的椅子也在這排，可入座）
                              // x0 由 '#' 改成 '.'：這是辦公區與西側玄關走道唯一的接口。
                              // 設計圖上這裡本來是牆，屬於【刻意偏離設計圖】的一格——擴建開的門。
        "#.############.#",   // 下排辦公桌佔 6~8 三列，四組工作站整排封到 x13
        "#.############.#",   // x13＝長桌 obj_24 東端桌面（實心 100%），原本漏封→NPC 穿桌
        "#.############.#",   // x14 實心像素為 0（只有一根隔板柱）＝桌東側走道，留通
        "#..............#",   // x14 原本是盆栽 obj_29，拿掉後成為右側南北通道的出口
        "##########.#####",
        "##########.#####",
        "#######.#......#",   // 老闆房：桌子北側整排是地板（原本誤封）
        "######..##..##.#",   // x12,13＝桌面；x14＝桌東側走道
        "######.###..#..#",   // x13＝老闆椅座位（可入座）、x14＝走道
        "######......####",
        "################",
    };

    // 實際使用的地圖＝西側新區 ⊕ 辦公區。'D' 在這裡就併成 '.'——尋路與驗證都只認 '#'/'.'，
    // 門的視覺與觸發是另一層的事，不該讓走位演算法認得第三種字元。
    static readonly string[] Collision =
        West.Zip(OfficeRows, (w, o) => w.Replace('D', '.').Replace('T', '#') + o).ToArray();

    // (名稱, cell x, cell y)，cell 以設計圖左上為 (0,0)
    // 「@動作」後綴：NPC 到點後執行（sit_up=背對鏡頭入座、sit_left/right=側面坐姿）
    //
    // ⚠️ sit_up 不是坐姿。素材只切得出兩個真坐姿（NPCSprite.sitLeft/sitRight，見
    // tools/make_character.py 的 SITS）；sit_up 在 NPCSprite.Pick() 是 fallback 到 idleUp——
    // 站著的背影。工位看起來像坐著，靠的是【椅子畫在人前面】(Y-sort by pivot) 擋住下半身。
    // 那個把戲只在「椅背朝鏡頭」時成立，所以：
    //   ✅ 排在桌子南側、椅背朝鏡頭的工位 → sit_up 可用
    //   ❌ 會議桌／圓桌那種人坐在桌子【對面】的擺法 → 會變成「站在桌子後面」，
    //      要改成長桌東西兩側對坐（sit_left ↔ sit_right），一邊三個。
    //
    // ⚠️ 這張表的座標是【辦公區本地】的（同 OfficeRows），建房時統一加 OfficeX。西側新區的點
    // 在下面的 WestWaypoints，用【絕對】座標。兩套系統是刻意的：這樣上面那些「x13＝長桌東端」
    // 的註記才對得回設計圖，不必每次移動辦公區就全部重抄一遍。
    // 放錯表不會靜默出錯——ValidateWaypoints() 會把它判成落在牆裡或不可達。
    static readonly (string name, int cx, int cy)[] OfficeWaypoints =
    {
        ("chair_1@sit_up", 4, 5), ("chair_2@sit_up", 7, 5), ("chair_3@sit_up", 10, 5), // 上排座位
        ("chair_4@sit_up", 3, 9), ("chair_5@sit_up", 6, 9),
        ("chair_6@sit_up", 9, 9),     // 下排南側座位（chair_7 的椅子 obj_28 已隨第 4 組移除，坐上去是空氣）
        ("chair_8@sit_right", 7, 12), // 左下房橘椅（椅背在西，坐姿面東）
        ("boss_1", 11, 14),           // 老闆房西側走道（等審批時站這裡罰站）
        ("boss_seat@sit_left", 13, 14),  // 老闆桌對面的那張椅子（椅背在東→坐姿面西對螢幕）
        ("cooler_1", 9, 12),          // 飲水機前（站著）
        ("board_1@face_up", 12, 2),   // 右牆白板(obj_12)前——折線圖那面畫在隔間後方，站不進去
        // 站立式會議：協作模式開會時大家聚到白板前。刻意【不做長桌會議室】——量過碰撞圖，
        // 16×17 已經飽和，沒有一塊空地放得下長桌；硬塞就得重排家具、改碰撞圖，而每次改碰撞圖
        // 就可能再犯一次「出生點落在牆裡」。這個規模的團隊本來就是站在白板前開短會。
        ("meet_1@face_up", 13, 2), ("meet_2@face_up", 14, 2),
        ("meet_3@face_up", 12, 3), ("meet_4@face_up", 12, 4),
        // 各工位旁的站位：委派時主 agent 走過來，面向坐著的同事
        ("side_1@face_left", 5, 5), ("side_2@face_left", 8, 5), ("side_3@face_left", 11, 5),
        ("side_4@face_right", 2, 9), ("side_5@face_right", 5, 9),
        ("side_6@face_right", 8, 9),
        ("printer_1", 13, 5),         // 印表機前（站著）
    };

    // 西側新區的互動點（【絕對】座標，不加 OfficeX）。
    //
    // 會議室是六人長桌【東西對坐】——這是上面 sit_up 那段推出來的唯一可行擺法：素材只有
    // sitLeft/sitRight 兩個真坐姿，人坐在桌子「對面」會變成站在桌後。長桌放 x2-3，
    // 座位就落在 x1（面東→sit_right）與 x4（面西→sit_left），一邊三個、正好六個人設一人一位。
    static readonly (string name, int cx, int cy)[] WestWaypoints =
    {
        // 會議室在【右】、長桌 x8-9：座位 x7（面東）與 x10（面西），一邊三個
        ("meet_a1@sit_right", 7, 2), ("meet_a2@sit_right", 7, 3), ("meet_a3@sit_right", 7, 4),
        ("meet_b1@sit_left", 10, 2), ("meet_b2@sit_left", 10, 3), ("meet_b3@sit_left", 10, 4),
        ("reception_1@face_left", 5, 3),  // 櫃檯前（訪客站這裡，面向櫃檯）
        ("entrance@face_up", 6, 9),       // 大門【外】側：進出的人站這裡，也是開門的觸發點
        ("lobby_1", 4, 11), ("lobby_2", 8, 11),      // 訪客等候區
        ("vending_1", 3, 13), ("cooler_2", 6, 13),   // 販賣機／飲水機前
    };

    // 建房與驗證都用這一份：辦公區位移後 ⊕ 西區原樣。合併在這裡做一次，別讓呼叫端各自加 OfficeX。
    static IEnumerable<(string name, int cx, int cy)> AllWaypoints() =>
        OfficeWaypoints.Select(w => (w.name, w.cx + OfficeX, w.cy)).Concat(WestWaypoints);

    [MenuItem("Tools/Build Room")]
    public static void Build()
    {
        // 先 Refresh 再 ImportAsset：兩者不一樣。ImportAsset 只重匯【資料庫已經知道】的資產，
        // 而 tools/compose_room.py 是從 Unity 外面寫檔進來的——Unity 沒被聚焦時根本不知道那些
        // 檔案存在，ImportAsset 掃不到，接著 FindSprite 查無、Build 整批放棄、畫面毫無變化。
        AssetDatabase.Refresh();
        AssetDatabase.ImportAsset(SpriteRoot,
            ImportAssetOptions.ImportRecursive | ImportAssetOptions.ForceUpdate);

        // 兩塊拼起來的地圖，最容易錯的是「某一列的西區少打一格」——那會讓整列往左位移一格，
        // 而畫面上看起來只是「牆的位置怪怪的」。所以驗的是【每列等寬】與【西區寬度＝OfficeX】。
        if (West.Length != OfficeRows.Length || West.Any(r => r.Length != OfficeX)
            || OfficeRows.Any(r => r.Length != 16))
        {
            Debug.LogError($"RoomBuilder: 碰撞地圖拼不起來——West 要 {OfficeRows.Length} 列 × {OfficeX} 格、" +
                           "OfficeRows 要 16 格寬，逐列檢查是不是有一列多打或少打。");
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
        var westAsset = AssetDatabase.LoadAssetAtPath<TextAsset>(WestJson);
        var westData = westAsset == null ? null : JsonUtility.FromJson<FurnitureData>(westAsset.text);
        if (westData == null)
            Debug.LogWarning("RoomBuilder: 沒有 west.json——西側玄關/會議室不會有美術（跑 tools/compose_room.py 產）。走位與碰撞不受影響。");

        var need = new List<string> { "bg_base", "lz_wall" };
        need.AddRange(data.items.Where(it => !Hidden.Contains(it.name)).Select(it => it.name));
        if (westData != null)
        {
            need.Add("west_bg");
            need.AddRange(westData.items.Select(it => it.name));
        }
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
        bg.transform.localPosition = new Vector3(OfficeX, -artH / PPU, 0);
        var bgSr = bg.AddComponent<SpriteRenderer>();
        bgSr.sprite = sprites["bg_base"];
        bgSr.sortingOrder = -20;

        // 家具（每件獨立物件，位置直接來自 json）
        var props = new GameObject("Props");
        props.transform.SetParent(room.transform, false);

        // 兩區的 json 是同一個 schema，差別只有「要不要位移」——所以擺放只寫一份。
        void PlaceProps(Item[] list, float offsetX)
        {
            foreach (var it in list)
            {
                if (Hidden.Contains(it.name)) continue;
                var go = new GameObject(it.name);
                go.transform.SetParent(props.transform, false);
                go.transform.localPosition = new Vector3(offsetX + it.x / PPU, -(it.y + it.h) / PPU, 0);
                var sr = go.AddComponent<SpriteRenderer>();
                sr.sprite = sprites[it.name];
                sr.sortingOrder = 0;
                sr.spriteSortPoint = SpriteSortPoint.Pivot; // pivot=左下 → 以底邊 Y-sort
            }
        }
        PlaceProps(data.items, OfficeX);

        if (westData != null)
        {
            // 西區底圖：貼在世界原點（它的座標系就是地圖座標系，不必位移）
            var wbg = new GameObject("WestBackground");
            wbg.transform.SetParent(room.transform, false);
            wbg.transform.localPosition = new Vector3(0, -westData.artH / PPU, 0);
            var wsr = wbg.AddComponent<SpriteRenderer>();
            wsr.sprite = sprites["west_bg"];
            // -21（比辦公區底圖再低一層）：西區底圖刻意往東多畫一格墊在辦公區下面，補它
            // 西緣那段沒畫的透明區。兩張都用 -20 的話誰蓋誰是不定的，接縫會時好時壞。
            wsr.sortingOrder = -21;
            PlaceProps(westData.items, 0);
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
            for (int c = 0; c < Collision[r].Length; c++)
                if (Collision[r][c] == '#')
                    tm.SetTile(new Vector3Int(c, -(r + 1), 0), wallTile);

        // Waypoints
        var wps = new GameObject("Waypoints");
        foreach (var (name, cx, cy) in AllWaypoints())
        {
            var wp = new GameObject(name);
            wp.transform.SetParent(wps.transform);
            wp.transform.position = new Vector3(cx + 0.5f, -(cy + 0.5f), 0);
        }

        ValidateWaypoints();

        // 相機對準房間中心
        var cam = Camera.main;
        // 對準【整張】地圖中心（含西側新區），不是辦公區中心——先前寫死 8f 是原本 16 格寬的一半。
        if (cam != null)
            cam.transform.position = new Vector3(Collision[0].Length / 2f, -Collision.Length / 2f, -10f);

        UnityEditor.SceneManagement.EditorSceneManager.MarkSceneDirty(
            UnityEngine.SceneManagement.SceneManager.GetActiveScene());
        // 訊息要把【西區有沒有進來】講出來。先前只報辦公區的家具數，於是「西區整塊沒載入」
        // 跟「一切正常」印出來一模一樣——看 Console 也判斷不了，只能猜。
        Debug.Log($"RoomBuilder: 房間完成——地圖 {Collision[0].Length}×{Collision.Length}、"
                  + $"辦公區 {data.items.Length} 件家具（x+{OfficeX}）、"
                  + (westData == null ? "西區【無美術】" : $"西區 {westData.items.Length} 件家具 + 底圖 {westData.canvasW}×{westData.artH}px")
                  + $"、{AllWaypoints().Count()} 個 waypoint");
    }

    // 每次 Build 驗證：所有 waypoint 都落在可走格，且從出生點 BFS 可達（孤島直接報錯）
    static void ValidateWaypoints()
    {
        // 起點取辦公區的中央走道（OfficeRows 第 5 列一路開通）。寫死 (8,5) 的話，辦公區東移後
        // 那格會落到西區走道——這次剛好也連通，但「剛好」不是保證：起點要跟著 OfficeX 走。
        var seed = (OfficeX + 8, 5);
        var seen = new HashSet<(int, int)> { seed };
        var q = new Queue<(int, int)>();
        q.Enqueue(seed);
        while (q.Count > 0)
        {
            var (c, r) = q.Dequeue();
            foreach (var (dc, dr) in new[] { (1, 0), (-1, 0), (0, 1), (0, -1) })
            {
                var n = (c + dc, r + dr);
                // 邊界一律問 InBounds，不要在這裡自己寫一份。先前這行是 n.Item1 < 16——
                // 地圖加寬後起點 (18,5) 自己就在界外，BFS 一步都擴不出去，於是【全部】30 個
                // waypoint 都被報成不可達。同一個寬度判斷散在三處，改了兩處漏一處就是這下場。
                if (!seen.Contains(n) && InBounds(n.Item1, n.Item2)
                    && Collision[n.Item2][n.Item1] == '.')
                {
                    seen.Add(n);
                    q.Enqueue(n);
                }
            }
        }
        foreach (var (name, cx, cy) in AllWaypoints())
            if (!InBounds(cx, cy) || Collision[cy][cx] != '.' || !seen.Contains((cx, cy)))
                Debug.LogError($"RoomBuilder: waypoint {name} ({cx},{cy}) 不可達或在牆裡！");
    }

    /// 格座標在地圖範圍內嗎。【入口平權】：地圖寬度的判斷只有這一處，BFS、Walkable、
    /// waypoint 驗證都問它。先前寬度散在三個地方各寫一份 16，加寬地圖時改了兩處漏一處，
    /// 結果是 BFS 的起點自己就在界外、30 個 waypoint 全被判成不可達。
    static bool InBounds(int cx, int cy) =>
        cy >= 0 && cy < Collision.Length && cx >= 0 && cx < Collision[cy].Length;

    /// 世界座標落在可走格嗎。開放給 CharacterBuilder 驗出生點——碰撞圖校正過後，
    /// 出生點沒有跟著校，三個人被生在牆裡卻沒有任何東西報錯（實測：他們的走位指令
    /// 每一次都逾時，一次都沒抵達過）。驗 waypoint 卻不驗出生點，是這道防線的缺口。
    public static bool Walkable(Vector3 world)
    {
        int cx = Mathf.FloorToInt(world.x), cy = Mathf.FloorToInt(-world.y);
        return InBounds(cx, cy) && Collision[cy][cx] == '.';
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

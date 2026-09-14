using System.Linq;
using UnityEditor;
using UnityEngine;

// 每個 persona 一個 prefab（sprites 來自 Characters/<prefix>/）+ 場景各擺一隻
public static class CharacterBuilder
{
    const string CharRoot = "Assets/Sprites/LimeZu/Characters";
    const string EmoteRoot = "Assets/Sprites/LimeZu/Emotes";

    // 與 backend/personas/*.yaml 一一對應（多一個 persona 就在這加一行再 Build Characters）。
    //
    // 出生點 = 那個人【自己的工位】。這樣選有兩個好處：位置本來就被 ValidateWaypoints 驗過
    // 可走，而且開場畫面是「大家在自己位子上」而不是散在走道中央。
    //
    // ⚠ 舊座標把三個人生在第 6 列（y=-6.5），註解還寫著「第 5、6 兩列是中央走道」——那句
    // 後來就不成立了：碰撞圖校正時把上排隔間補封，第 6 列變成牆，出生點卻沒跟著校。
    // 結果小美、老王、小葵【從第一秒就站在牆裡】，走位指令每次都逾時，一次都沒抵達過
    // （實測 /agents 的 memory 全是「剛剛走去某處超時沒走到」）。
    // 現在 Build 會驗證，落在牆裡直接報錯，不再靜默生出走不動的人。
    static readonly (string prefix, Vector3 spawn)[] Personas =
    {
        ("p17", new Vector3(RoomBuilder.OfficeX + 4.5f, -5.5f, 0)),    // chair_1
        ("p01", new Vector3(RoomBuilder.OfficeX + 7.5f, -5.5f, 0)),    // chair_2
        ("p07", new Vector3(RoomBuilder.OfficeX + 10.5f, -5.5f, 0)),   // chair_3
        ("p05", new Vector3(RoomBuilder.OfficeX + 3.5f, -9.5f, 0)),    // chair_4
        ("p12", new Vector3(RoomBuilder.OfficeX + 6.5f, -9.5f, 0)),    // chair_5
        ("p08", new Vector3(RoomBuilder.OfficeX + 9.5f, -9.5f, 0)),    // chair_6
        ("p19", new Vector3(RoomBuilder.OfficeX + 13.5f, -14.5f, 0)),  // boss_seat
    };

    const string ScenePath = "Assets/Scenes/SampleScene.unity";   // WebGLBuilder 建的就是這一個

    [MenuItem("Tools/Build Characters")]
    public static void Build()
    {
        // 批次模式（-batchmode -executeMethod）開的是一個空的未命名場景，不是 SampleScene：在那裡 Find 不到任何
        // NPC、生出來的實例也存不進任何檔案——員工靠 prefab 覆寫僥倖沒事，總機從普通物件換成 prefab 實例就露餡了
        // （建了兩次場景檔一個位元組都沒變）。所以先把真正的場景打開，最後再存回去。
        if (Application.isBatchMode)
            UnityEditor.SceneManagement.EditorSceneManager.OpenScene(ScenePath);
        // 同 RoomBuilder：Refresh 讓資料庫先看到 Unity 外面寫進來的新檔，ImportAsset 才掃得到。
        AssetDatabase.Refresh();
        AssetDatabase.ImportAsset(CharRoot,
            ImportAssetOptions.ImportRecursive | ImportAssetOptions.ForceUpdate);
        // 先驗出生點再生人：生在牆裡的角色永遠走不到任何地方，而且畫面上看起來只是
        // 「站著不動」——跟閒置一模一樣，所以沒有人會發現。寧可 Build 直接紅字停下來。
        foreach (var (prefix, spawn) in Personas)
            if (!RoomBuilder.Walkable(spawn))
                Debug.LogError($"CharacterBuilder: {prefix} 的出生點 {spawn} 落在牆裡——" +
                               "他會卡在原地、所有走位都逾時。請改到可走格（建議用他自己的工位）。");
        foreach (var (prefix, spawn) in Personas) BuildOne(prefix, spawn);
        BuildReception();
        if (GameObject.Find("BrainGateway") == null)
            new GameObject("BrainGateway").AddComponent<BrainGateway>();
        UnityEditor.SceneManagement.EditorSceneManager.MarkSceneDirty(
            UnityEngine.SceneManagement.SceneManager.GetActiveScene());
        // 批次模式沒有人會按存檔：員工是 prefab 實例、場景不存也吃得到新 prefab，但總機從「場景裡的普通物件」
        // 換成 prefab 實例是【場景本身】的變更——不存的話 WebGL 建置（從磁碟讀場景）拿到的還是舊的那個。
        if (Application.isBatchMode)
            UnityEditor.SceneManagement.EditorSceneManager.SaveOpenScenes();
        Debug.Log($"CharacterBuilder: {Personas.Length + 1} 個 NPC 完成（含總機）");
    }

    // 總機小姐（小安，p10）：崗位在櫃檯後那一格——櫃檯叢集 obj_03 的書架、檯面、螢幕桌把那格四面圍住，
    // 美術上沒有走得出去的口，碰撞圖也是家具格（'T'）。所以她【不走位】：不掛 FakeBrain／NPCSeparation、
    // 不掛實體碰撞（在牆的 collider 裡會被物理推出去），Rigidbody 設 Kinematic 只為了 NPCMover 能用。
    // 其他都跟員工一樣：NPCSprite 的全套姿勢（接電話、看書、趴睡、遞交、受傷）、徽章、泡泡、對話框、NPCAgent。
    // 橋端用人設的 post: fixed 知道她不走位，走位一律換成姿勢（backend stays_put）。
    // 出生點刻意不過 Walkable 驗證：位置對準櫃檯 sprite 上挖的洞（D1_HOLES）：世界 px x52-68、腳底 y49 → (60/16, -49/16)。
    const string ReceptionPrefix = "p10";
    static readonly Vector3 ReceptionPost = new(3.75f, -3.0625f, 0);

    static void BuildReception() => BuildOne(ReceptionPrefix, ReceptionPost, physical: false, name: "NPC_reception");

    // 徽章素材【全員共用】（不像角色幀是每人一套），掃一次快取起來。
    // 檔名格式 <name>_<幀號>.png，見 tools/make_emotes.py。
    static NPCEmote.Set[] emoteCache;

    static NPCEmote.Set[] LoadEmotes()
    {
        if (emoteCache != null) return emoteCache;
        if (!AssetDatabase.IsValidFolder(EmoteRoot)) return emoteCache = new NPCEmote.Set[0];
        var byName = new System.Collections.Generic.SortedDictionary<string,
            System.Collections.Generic.List<Sprite>>();
        foreach (var guid in AssetDatabase.FindAssets("t:Sprite", new[] { EmoteRoot }))
        {
            var sp = AssetDatabase.LoadAssetAtPath<Sprite>(AssetDatabase.GUIDToAssetPath(guid));
            if (sp == null) continue;
            int u = sp.name.LastIndexOf('_');            // 砍掉尾巴的幀號
            if (u <= 0) continue;
            string key = sp.name.Substring(0, u);
            if (!byName.TryGetValue(key, out var list))
                byName[key] = list = new System.Collections.Generic.List<Sprite>();
            list.Add(sp);
        }
        var sets = new System.Collections.Generic.List<NPCEmote.Set>();
        foreach (var kv in byName)
        {
            kv.Value.Sort((a, b) => string.CompareOrdinal(a.name, b.name));
            sets.Add(new NPCEmote.Set { name = kv.Key, frames = kv.Value.ToArray() });
        }
        return emoteCache = sets.ToArray();
    }

    // physical=false：固定崗位的人（總機）——沒有碰撞、不被推、不亂晃；其餘一律相同
    static void BuildOne(string prefix, Vector3 spawn, bool physical = true, string name = null)
    {
        string folder = $"{CharRoot}/{prefix}";
        var all = AssetDatabase.FindAssets("t:Sprite", new[] { folder })
            .Select(g => AssetDatabase.LoadAssetAtPath<Sprite>(AssetDatabase.GUIDToAssetPath(g)))
            .Where(s => s != null).ToArray();
        Sprite[] Load(string key) =>
            all.Where(s => s.name.Contains($"_{key}_")).OrderBy(FrameNo).ToArray();

        var idleDown = Load("idle_down");
        if (idleDown.Length == 0)
        {
            Debug.LogError($"CharacterBuilder: {folder} 沒有幀，先跑 tools/make_character.py");
            return;
        }

        string npcName = name ?? $"NPC_{prefix}";
        var old = GameObject.Find(npcName);
        if (old != null) Object.DestroyImmediate(old);

        var go = new GameObject(npcName);
        var sr = go.AddComponent<SpriteRenderer>();
        sr.spriteSortPoint = SpriteSortPoint.Pivot; // pivot=腳底 → 與家具正確 Y-sort
        sr.sprite = idleDown[0];

        var rb = go.AddComponent<Rigidbody2D>();
        rb.gravityScale = 0;
        rb.freezeRotation = true;
        rb.collisionDetectionMode = CollisionDetectionMode2D.Continuous;
        rb.interpolation = RigidbodyInterpolation2D.Interpolate;
        if (!physical) rb.bodyType = RigidbodyType2D.Kinematic;   // 站在家具格裡：不能有物理，否則被推出來

        CircleCollider2D body = null;
        if (physical)
        {
            body = go.AddComponent<CircleCollider2D>();
            body.radius = 0.18f; // 格心到牆間隙 0.25，留餘裕不磨牆
            body.offset = new Vector2(0, 0.15f);
            body.sharedMaterial = GetSlipperyMaterial();

            var sensor = go.AddComponent<CircleCollider2D>(); // 相遇偵測圈（< 1 tile）
            sensor.isTrigger = true;
            sensor.radius = 0.9f;
            sensor.offset = new Vector2(0, 0.15f);
        }

        go.AddComponent<NPCMover>();
        var anim = go.AddComponent<NPCSprite>();
        anim.idleDown = idleDown;
        anim.idleUp = Load("idle_up");
        anim.idleRight = Load("idle_right");
        anim.idleLeft = Load("idle_left");
        anim.walkDown = Load("walk_down");
        anim.walkUp = Load("walk_up");
        anim.walkRight = Load("walk_right");
        anim.walkLeft = Load("walk_left");
        anim.sitRight = Load("sit_right");
        anim.sitLeft = Load("sit_left");
        anim.phone = Load("phone");
        anim.sleep = Load("sleep");
        anim.book = Load("book");
        anim.giftUp = Load("gift_up");
        anim.giftDown = Load("gift_down");
        anim.giftRight = Load("gift_right");
        anim.giftLeft = Load("gift_left");
        // 一次性動作四向各一段，名稱就是檔名段（hurt_up…）——橋端送什麼字串，這裡就找什麼
        anim.clips = OneShots
            .SelectMany(a => new[] { "up", "down", "left", "right" }.Select(d => $"{a}_{d}"))
            .Select(k => new NPCSprite.Clip { name = k, frames = Load(k) })
            .Where(c => c.frames.Length > 0).ToArray();
        if (anim.clips.Length < OneShots.Length * 4)
            Debug.LogWarning($"CharacterBuilder: {prefix} 一次性動作只有 {anim.clips.Length}/{OneShots.Length * 4} 段——" +
                             "重跑 tools/make_character.py 再複製 hurt/pick_up/lift/throw 的幀進來");

        // 頭邊的狀態徽章（持續顯示；跟泡泡分工見 NPCEmote 的說明）。
        // 位置在右上角、略疊住頭：泡泡在正上方 2.15、對話框在 2.55，錯開才不會互相蓋。
        // emote 的 pivot 是左下（路徑含 /LimeZu/ 但不含 /Characters/），所以這是左下角座標。
        var emoteGo = new GameObject("Emote");
        emoteGo.transform.SetParent(go.transform, false);
        emoteGo.transform.localPosition = new Vector3(0.25f, 1.5f, 0);
        var emoteSr = emoteGo.AddComponent<SpriteRenderer>();
        emoteSr.sortingOrder = 55;          // 泡泡 50 之上、對話框 59 之下
        var emote = emoteGo.AddComponent<NPCEmote>();
        emote.sets = LoadEmotes();
        if (emote.sets.Length == 0)
            Debug.LogWarning("CharacterBuilder: 沒有情緒徽章素材——先跑 tools/make_emotes.py，"
                             + "再把 limezu/_extracted/emotes 複製到 " + EmoteRoot);

        // 頭上泡泡
        var bubbleGo = new GameObject("Bubble");
        bubbleGo.transform.SetParent(go.transform, false);
        bubbleGo.transform.localPosition = new Vector3(0.1f, 2.15f, 0);
        var bubbleSr = bubbleGo.AddComponent<SpriteRenderer>();
        bubbleSr.sprite = FindSprite("bubble");
        bubbleSr.sortingOrder = 50;
        bubbleSr.enabled = false;

        // 頭上文字對話框（TextMesh 動態字型 + 9-slice 白底）
        var speechGo = new GameObject("Speech");
        speechGo.transform.SetParent(go.transform, false);
        speechGo.transform.localPosition = new Vector3(0, 2.55f, 0);
        var panelGo = new GameObject("Panel");
        panelGo.transform.SetParent(speechGo.transform, false);
        var panelSr = panelGo.AddComponent<SpriteRenderer>();
        panelSr.sprite = FindSprite("speech_panel");
        panelSr.drawMode = SpriteDrawMode.Sliced;
        panelSr.sortingOrder = 59;
        var textGo = new GameObject("Text");
        textGo.transform.SetParent(speechGo.transform, false);
        var tm = textGo.AddComponent<TextMesh>();
        // 內建的 LegacySystemFont.ttf 在新版 Unity 已經拿掉（每次 Build 都噴三行紅字）。
        // 直接用 Resources/OfficeFont——執行期 NPCSpeech 本來就會換成它，這裡先用同一份，
        // 編輯器預覽與實機才一致。
        tm.font = Resources.Load<Font>("OfficeFont");
        tm.fontSize = 64;
        tm.characterSize = 0.045f;
        tm.anchor = TextAnchor.MiddleCenter;
        tm.alignment = TextAlignment.Center;
        tm.color = new Color32(45, 50, 68, 255);
        if (tm.font != null) textGo.GetComponent<MeshRenderer>().sharedMaterial = tm.font.material;
        textGo.GetComponent<MeshRenderer>().sortingOrder = 60;
        var speech = go.AddComponent<NPCSpeech>();
        speech.text = tm;
        speech.panel = panelSr;
        panelGo.SetActive(false);
        textGo.SetActive(false);

        var agent = go.AddComponent<NPCAgent>();
        agent.agentId = prefix;
        var meeting = go.AddComponent<NPCMeeting>();
        meeting.bubble = bubbleSr;
        meeting.body = body;   // 固定崗位的人是 null（NPCMeeting.Start 有守衛）
        if (physical)
        {
            go.AddComponent<FakeBrain>();
            go.AddComponent<NPCSeparation>();
        }

        if (!AssetDatabase.IsValidFolder("Assets/Prefabs"))
            AssetDatabase.CreateFolder("Assets", "Prefabs");
        var prefab = PrefabUtility.SaveAsPrefabAsset(go, $"Assets/Prefabs/{npcName}.prefab");
        Object.DestroyImmediate(go);

        var inst = (GameObject)PrefabUtility.InstantiatePrefab(prefab);
        inst.transform.position = spawn;
    }

    // 只切了還沒接線的（pick_up／lift／throw）也先裝進 prefab：貴的是重建 prefab 與 WebGL，一次做完。
    static readonly string[] OneShots = { "hurt", "pick_up", "lift", "throw" };

    // 幀序號在檔名最後一段（p01_lift_up_13）；序號兩位數的動作靠字串排會亂。
    static int FrameNo(Sprite s) =>
        int.TryParse(s.name.Substring(s.name.LastIndexOf('_') + 1), out var n) ? n : 0;

    static PhysicsMaterial2D GetSlipperyMaterial()
    {
        const string path = "Assets/Physics/NPC_Slippery.physicsMaterial2D";
        var mat = AssetDatabase.LoadAssetAtPath<PhysicsMaterial2D>(path);
        if (mat == null)
        {
            if (!AssetDatabase.IsValidFolder("Assets/Physics"))
                AssetDatabase.CreateFolder("Assets", "Physics");
            mat = new PhysicsMaterial2D("NPC_Slippery") { friction = 0f, bounciness = 0f };
            AssetDatabase.CreateAsset(mat, path);
        }
        return mat;
    }

    static Sprite FindSprite(string name)
    {
        foreach (var guid in AssetDatabase.FindAssets($"t:Sprite {name}", new[] { "Assets/Sprites" }))
            foreach (var s in AssetDatabase.LoadAllAssetsAtPath(AssetDatabase.GUIDToAssetPath(guid)).OfType<Sprite>())
                if (s.name == name) return s;
        Debug.LogError($"CharacterBuilder: 找不到 sprite '{name}'");
        return null;
    }
}

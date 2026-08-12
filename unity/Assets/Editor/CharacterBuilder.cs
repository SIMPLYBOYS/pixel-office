using System.Linq;
using UnityEditor;
using UnityEngine;

// 每個 persona 一個 prefab（sprites 來自 Characters/<prefix>/）+ 場景各擺一隻
public static class CharacterBuilder
{
    const string CharRoot = "Assets/Sprites/LimeZu/Characters";

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
        ("p19", new Vector3(RoomBuilder.OfficeX + 13.5f, -14.5f, 0)),  // boss_seat
    };

    [MenuItem("Tools/Build Characters")]
    public static void Build()
    {
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
        Debug.Log($"CharacterBuilder: {Personas.Length} 個 NPC 完成");
    }

    // 總機小姐：櫃檯後的常駐角色，【不在】Personas 名單裡——她不投影任何 agent 狀態，
    // 所以不掛 NPCAgent/FakeBrain/碰撞那一套（掛了就會被生活迴圈派去亂晃，總機離崗）。
    // 出生點刻意不過 Walkable 驗證：她的崗位本來就在櫃檯的家具格（'T'）裡，不用走路。
    //
    // 她跟被清掉的「畫死西裝男」（compose_room 的 D1_PATCHES）差在哪：她是活的物件，
    // 有待機動畫，透過櫃檯 sprite 上挖的洞（D1_HOLES）露出來——桌面遮腰、書架遮兩側。
    // 位置對準那個洞：世界 px x52-68、腳底 y49 → (60/16, -49/16)。
    const string ReceptionPrefix = "p10";   // 盤髮白衫的那位（01/05/07/12/17/19 已是員工）
    static readonly Vector3 ReceptionPost = new(3.75f, -3.0625f, 0);

    static void BuildReception()
    {
        var old = GameObject.Find("NPC_reception");
        if (old != null) Object.DestroyImmediate(old);

        string folder = $"{CharRoot}/{ReceptionPrefix}";
        var all = AssetDatabase.FindAssets("t:Sprite", new[] { folder })
            .Select(g => AssetDatabase.LoadAssetAtPath<Sprite>(AssetDatabase.GUIDToAssetPath(g)))
            .Where(s => s != null).ToArray();
        Sprite[] Frames(string key) => all.Where(s => s.name.Contains(key))
                                          .OrderBy(s => s.name).ToArray();
        var idle = Frames("_idle_down_");
        if (idle.Length == 0)
        {
            Debug.LogError($"CharacterBuilder: {folder} 沒有幀——先跑 tools/make_character.py 10，" +
                           "再把 limezu/_extracted/characters/p10 複製到 " + CharRoot);
            return;
        }

        var go = new GameObject("NPC_reception");
        var sr = go.AddComponent<SpriteRenderer>();
        sr.spriteSortPoint = SpriteSortPoint.Pivot;   // pivot=腳底 → 櫃檯(-5)畫在她(-3.06)前面
        var loop = go.AddComponent<SpriteLoop>();
        loop.frames = idle;
        loop.extra = Frames("_phone_");   // 偶爾接電話（純隨機的裝飾，見 SpriteLoop 註解）
        go.transform.position = ReceptionPost;
    }

    static void BuildOne(string prefix, Vector3 spawn)
    {
        string folder = $"{CharRoot}/{prefix}";
        var all = AssetDatabase.FindAssets("t:Sprite", new[] { folder })
            .Select(g => AssetDatabase.LoadAssetAtPath<Sprite>(AssetDatabase.GUIDToAssetPath(g)))
            .Where(s => s != null).ToArray();
        Sprite[] Load(string key) =>
            all.Where(s => s.name.Contains($"_{key}_")).OrderBy(s => s.name).ToArray();

        var idleDown = Load("idle_down");
        if (idleDown.Length == 0)
        {
            Debug.LogError($"CharacterBuilder: {folder} 沒有幀，先跑 tools/make_character.py");
            return;
        }

        string npcName = $"NPC_{prefix}";
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

        var body = go.AddComponent<CircleCollider2D>();
        body.radius = 0.18f; // 格心到牆間隙 0.25，留餘裕不磨牆
        body.offset = new Vector2(0, 0.15f);
        body.sharedMaterial = GetSlipperyMaterial();

        var sensor = go.AddComponent<CircleCollider2D>(); // 相遇偵測圈（< 1 tile）
        sensor.isTrigger = true;
        sensor.radius = 0.9f;
        sensor.offset = new Vector2(0, 0.15f);

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
        meeting.body = body;
        go.AddComponent<FakeBrain>();
        go.AddComponent<NPCSeparation>();

        if (!AssetDatabase.IsValidFolder("Assets/Prefabs"))
            AssetDatabase.CreateFolder("Assets", "Prefabs");
        var prefab = PrefabUtility.SaveAsPrefabAsset(go, $"Assets/Prefabs/{npcName}.prefab");
        Object.DestroyImmediate(go);

        var inst = (GameObject)PrefabUtility.InstantiatePrefab(prefab);
        inst.transform.position = spawn;
    }

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

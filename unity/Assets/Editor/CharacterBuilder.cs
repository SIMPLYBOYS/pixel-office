using System.Linq;
using UnityEditor;
using UnityEngine;

// 每個 persona 一個 prefab（sprites 來自 Characters/<prefix>/）+ 場景各擺一隻
public static class CharacterBuilder
{
    const string CharRoot = "Assets/Sprites/LimeZu/Characters";

    // 與 backend/personas/*.yaml 一一對應（多一個 persona 就在這加一行再 Build Characters）。
    // spawn 必須落在 RoomBuilder.Collision 的 '.' 格：第 5、6 兩列是中央走道，整排可走。
    static readonly (string prefix, Vector3 spawn)[] Personas =
    {
        ("p17", new Vector3(8.5f, -5.5f, 0)),
        ("p01", new Vector3(4.5f, -6.5f, 0)),
        ("p07", new Vector3(11.5f, -6.5f, 0)),
        ("p05", new Vector3(2.5f, -5.5f, 0)),
        ("p12", new Vector3(6.5f, -6.5f, 0)),
        ("p19", new Vector3(13.5f, -5.5f, 0)),
    };

    [MenuItem("Tools/Build Characters")]
    public static void Build()
    {
        AssetDatabase.ImportAsset(CharRoot,
            ImportAssetOptions.ImportRecursive | ImportAssetOptions.ForceUpdate);
        foreach (var (prefix, spawn) in Personas) BuildOne(prefix, spawn);
        if (GameObject.Find("BrainGateway") == null)
            new GameObject("BrainGateway").AddComponent<BrainGateway>();
        UnityEditor.SceneManagement.EditorSceneManager.MarkSceneDirty(
            UnityEngine.SceneManagement.SceneManager.GetActiveScene());
        Debug.Log($"CharacterBuilder: {Personas.Length} 個 NPC 完成");
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

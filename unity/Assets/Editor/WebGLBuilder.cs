using UnityEditor;
using UnityEngine;

// Tools → Build WebGL：輸出到 unity/Builds/WebGL，由 FastAPI 橋伺服（/unity）。
// 壓縮關掉：StaticFiles 不會送 Content-Encoding 標頭，Brotli/Gzip 版瀏覽器載不起來；
// localhost 傳輸量無所謂，正式部署再換 CDN + 壓縮。
public static class WebGLBuilder
{
    [MenuItem("Tools/Build WebGL (辦公室網頁版)")]
    public static void Build()
    {
        PlayerSettings.WebGL.compressionFormat = WebGLCompressionFormat.Disabled;
        // ⛔ 關掉 data caching：Unity 會把 WebGL.data 存進 IndexedDB，快取鍵是
        // companyName/productName/productVersion——三者固定不變的話，每次重建都是同一把鍵，
        // 瀏覽器就可能拿【舊的 .data】配【新的 .wasm】→ RuntimeError: memory access out of bounds。
        // 本機開發重建頻繁，快取省的那點載入時間遠不值這個坑。
        PlayerSettings.WebGL.dataCaching = false;
        // 版本號帶建置時間：就算哪天想開快取，鍵也會每次不同（並讓「載到的是哪一版」看得出來）
        PlayerSettings.bundleVersion = System.DateTime.Now.ToString("yyyyMMdd.HHmm");
        // 辦公室是狀態看板：使用者多半在別的視窗操作（終端機、Slack），焦點不在瀏覽器時
        // 若讓 Unity 暫停，NPC 就整個凍住、指令堆著沒人處理（實測踩過兩次）。
        PlayerSettings.runInBackground = true;
        // NativeWebSocket 的 dynCall 相容性改在內嵌套件的 jslib 修（makeDynCall 巨集，
        // 見 Packages/com.endel.nativewebsocket），不需要 emscripten 旗標。
        var report = BuildPipeline.BuildPlayer(
            new[] { "Assets/Scenes/SampleScene.unity" },
            "Builds/WebGL", BuildTarget.WebGL, BuildOptions.None);
        Debug.Log($"WebGL build: {report.summary.result}，輸出 unity/Builds/WebGL，" +
                  "瀏覽器開 http://localhost:8123/shell/");
    }
}

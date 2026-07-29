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
        // NativeWebSocket 的 dynCall 相容性改在內嵌套件的 jslib 修（makeDynCall 巨集，
        // 見 Packages/com.endel.nativewebsocket），不需要 emscripten 旗標。
        var report = BuildPipeline.BuildPlayer(
            new[] { "Assets/Scenes/SampleScene.unity" },
            "Builds/WebGL", BuildTarget.WebGL, BuildOptions.None);
        Debug.Log($"WebGL build: {report.summary.result}，輸出 unity/Builds/WebGL，" +
                  "瀏覽器開 http://localhost:8123/shell/");
    }
}

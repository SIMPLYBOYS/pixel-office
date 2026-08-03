using UnityEditor;

// 泡泡中文字型：把 Assets/Resources/OfficeFont.otf 匯入成「只含指定字元」的圖集字型。
//
// 為什麼不用 Dynamic：Dynamic 會把整份 16MB 字型塞進 build（WebGL 下載爆炸），而且
// 編輯器能靠 macOS 系統字型補字、WebGL 沒有字型可退——中文就整個空白（實測踩過）。
// CustomSet 只烘我們真的會顯示的字，build 只多一張小圖集。
//
// 加新的泡泡詞就要同步加字進 Chars（沒烘到的字會是空白）。
public class OfficeFontImporter : AssetPostprocessor
{
    // 狀態泡泡用字（含標點與符號）；生活模擬的自由對話不在此列——純投影模式下用不到。
    // 符號限這份字型有字形的：✓ ⚠ ★ ● ▶ →（✔ ✗ ▸ 與所有 emoji 都沒有字形，用了會空白）。
    // 英數是為了泡泡顯示【工具名】（bash / read_file / write_file…）——每次做的事不一樣，
    // 泡泡才有資訊量；缺了英數的話工具名會整條空白。
    const string Chars =
        "接到任務執行中出錯了回報完成任務中斷交辦支援等待審批放行駁回失聯沒回應開工休息思考" +
        "。，、！？…()：0123456789✓⚠★●▶→" +
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-.";

    // 字表改了要跟著 +1，Unity 才會自動重匯入把新字烘進圖集（否則得手動 Reimport）。
    public override uint GetVersion() => 4;

    void OnPreprocessAsset()
    {
        if (!assetPath.EndsWith("Resources/OfficeFont.otf")) return;
        var f = (TrueTypeFontImporter)assetImporter;
        f.fontTextureCase = FontTextureCase.CustomSet;
        f.customCharacters = Chars;
        f.fontSize = 64;          // 與 CharacterBuilder 的 TextMesh fontSize 一致
        f.includeFontData = false; // 只帶圖集，不把整份字型塞進 build
    }
}

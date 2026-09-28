using UnityEditor;

// 泡泡中文字型：Assets/Resources/OfficeFont.otf 是【子集字型】（tools/subset_font.py 從 Noto Sans CJK TC
// 切出 Big5 常用字 5401 字＋標點＋英數，約 1.4MB），這裡把它匯入成動態字型、字型資料跟著 build 走。
//
// 以前是「只烘指定字元」的圖集字型：只有幾十個狀態詞，生活模擬的自由對話畫不出來（WebGL 會是空白）。
// 動態字型要帶字型資料——WebGL 沒有系統字型可退；整份 16MB 帶進去太胖，所以先切子集。
// 子集以外的罕用字仍會是空白（上限：Big5 第一級常用字；要更多字就改 subset_font.py 的字表，體積跟著長）。
public class OfficeFontImporter : AssetPostprocessor
{
    // 匯入設定改了要跟著 +1，Unity 才會自動重匯入。
    public override uint GetVersion() => 5;

    void OnPreprocessAsset()
    {
        if (!assetPath.EndsWith("Resources/OfficeFont.otf")) return;
        var f = (TrueTypeFontImporter)assetImporter;
        f.fontTextureCase = FontTextureCase.Dynamic;
        f.fontSize = 64;           // 與 CharacterBuilder 的 TextMesh fontSize 一致
        f.includeFontData = true;  // WebGL 沒有系統字型可退：字形要跟著 build 走
    }
}

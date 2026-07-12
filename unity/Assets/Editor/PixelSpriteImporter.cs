using UnityEditor;
using UnityEngine;

// ponytail: 只管匯入設定；LimeZu spritesheet 進來時再加 16×16 自動切格（ISpriteEditorDataProvider）
class PixelSpriteImporter : AssetPostprocessor
{
    void OnPreprocessTexture()
    {
        if (!assetPath.StartsWith("Assets/Sprites")) return;
        var importer = (TextureImporter)assetImporter;
        importer.textureType = TextureImporterType.Sprite;
        importer.spriteImportMode = SpriteImportMode.Single; // 模板預設 Multiple 且無切格資料 → 0 個 sprite
        importer.spritePixelsPerUnit = 16;
        importer.filterMode = FilterMode.Point;
        importer.textureCompression = TextureImporterCompression.Uncompressed;
        importer.mipmapEnabled = false;

        // 對話框底圖：9-slice 邊界（拉伸不變形圓角）
        if (assetPath.EndsWith("speech_panel.png"))
            importer.spriteBorder = new Vector4(4, 4, 4, 4);

        // LimeZu 件已裁到 16 倍數、底部對齊 → pivot 左下，配合 tilemap tileAnchor (0,0)
        // 角色例外：pivot 腳底中心（transform = 腳的位置，Y-sort 以腳判定）
        if (assetPath.Contains("/LimeZu/"))
        {
            var settings = new TextureImporterSettings();
            importer.ReadTextureSettings(settings);
            settings.spriteAlignment = (int)(assetPath.Contains("/Characters/")
                ? SpriteAlignment.BottomCenter : SpriteAlignment.BottomLeft);
            importer.SetTextureSettings(settings);
        }
    }
}

# 第三方來源與授權

Pixel Office 的 `LICENSE`（MIT）**只涵蓋本專案自己寫的程式碼**。以下第三方素材與依賴各有自己的
授權，不因為被本專案使用而變成 MIT。

本檔案只描述來源與邊界，**不包含任何第三方素材的內容**。

## 像素素材：LimeZu（付費，不隨 repo 散布）

| 項目 | 內容 |
|------|------|
| 作者 | LimeZu |
| 來源 | https://limezu.itch.io |
| 使用的素材包 | Modern Interiors、Modern Office（Revamped） |
| 是否包含在本 repository | **否** |
| 是否包含在任何 release／build | **否** |
| 取得方式 | 使用者自行向 itch.io 購買合法副本 |

授權要點（以素材包內的 `LICENSE.txt` 為準）：

- 可編輯，可用於商業或非商業專案。
- **不可轉售或散布原始素材，也不可轉售或散布修改後的素材。**
- Modern Interiors **要求標示 credit：`limezu.itch.io`**；Modern Office 的 credit 是 appreciated，
  本專案一律標示 LimeZu。

因此下列路徑一律不進版控（見 `.gitignore`）：

```
limezu/                          # 購買的原始素材
unity/Assets/Sprites/LimeZu/     # 由 tools/ 重生的衍生物
unity/Builds/                    # 可能內含素材的 build 產物
backend/avatars/                 # 從角色圖抽出的頭像（衍生物）
```

衍生物同樣受原授權拘束：裁切、改色、重新拼裝都不會讓它變成自有素材。

## 展示影片（`docs/media/`）

`pixel-office-demo.mp4` 與它的 GIF 版 `pixel-office-demo.gif` **有進版控**，是這份文件裡唯一例外：
它們是辦公室實際畫面的錄影與截圖，畫面裡看得到 LimeZu 素材，用途僅限展示本專案。
它們不是素材檔（拿不回原始圖塊），也不適用 MIT。

影片裡的聲音：

| 項目 | 來源 | 授權 |
|------|------|------|
| 配樂 | repo 裡的版本**沒有配樂**。對外發布用的版本另配 ende.app「Happy Beats / Business Moves」，授權尚未確認，所以不進 repo | — |
| 音效 | [Kenney](https://kenney.nl/)（按鍵、點擊、卡片、撞擊音） | Kenney 素材一般為 CC0，以原素材包標示為準 |

影片的製作檔（企劃、Hyperframes composition）在本機 `brag-output/`，內含素材衍生物，不進版控。

## 字型

| 項目 | 內容 |
|------|------|
| 字型 | Noto Sans CJK TC Regular |
| 授權 | SIL Open Font License 1.1 |
| 來源 | https://github.com/notofonts/noto-cjk |
| 是否包含在本 repository | 否——由 `tools/get_font.sh` 自行下載到 `unity/Assets/Resources/` |

## Unity 套件

Unity Editor 與其官方套件（`com.unity.*`，見 `unity/Packages/manifest.json`）依 Unity 的授權條款使用。

| 套件 | 版本 | 授權 | 來源 |
|------|------|------|------|
| NativeWebSocket（`com.endel.nativewebsocket`） | 1.1.6 | Apache License 2.0 | https://github.com/endel/NativeWebSocket |

NativeWebSocket 是**唯一隨本 repo 散布的第三方程式碼**：它以內嵌套件形式放在
`unity/Packages/com.endel.nativewebsocket/`（WebGL 的 jslib 相容性修改在那裡），有進版控。
授權全文與著作權宣告放在該目錄的 `LICENSE`（Copyright 2019 Endel Dreyer、2018 Jiri Hybek）——
Apache-2.0 要求散布時附上授權副本並保留宣告。

## Python 依賴

由 `backend/requirements.txt` 安裝，不隨 repo 散布原始碼：

| 套件 | 授權 |
|------|------|
| FastAPI | MIT |
| uvicorn（含 `[standard]` 額外依賴） | BSD-3-Clause |
| anthropic（Python SDK） | MIT |
| PyYAML | MIT |
| python-dotenv | BSD-3-Clause |

## 自製素材

`unity/Assets/Sprites/Placeholders/`、`unity/Assets/Sprites/UI/` 與 `unity/Assets/pixel_test.png`
是本專案自製的暫代圖，隨 repo 散布，適用 MIT。它們不是 LimeZu 素材的裁切或衍生物。

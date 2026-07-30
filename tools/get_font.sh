#!/usr/bin/env bash
# 取得泡泡中文字型：Noto Sans CJK TC Regular（SIL Open Font License）。
# 檔案 16MB 不進 git（見 .gitignore），換機器跑一次即可；Unity 端只會把
# OfficeFontImporter 指定的那幾十個字烘成圖集，build 不會被字型拖胖。
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/unity/Assets/Resources/OfficeFont.otf"
URL="https://github.com/notofonts/noto-cjk/releases/download/Sans2.004/09_NotoSansCJKtc.zip"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "下載 Noto Sans CJK TC…（約 95MB 壓縮檔）"
curl -sL -o "$TMP/noto.zip" "$URL"
unzip -o -q "$TMP/noto.zip" NotoSansCJKtc-Regular.otf -d "$TMP"
mkdir -p "$(dirname "$DEST")"
cp "$TMP/NotoSansCJKtc-Regular.otf" "$DEST"
echo "✓ $DEST"
echo "接著在 Unity 等它匯入完成，再 Tools → Build WebGL"

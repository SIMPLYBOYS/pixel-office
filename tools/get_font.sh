#!/usr/bin/env bash
# 取得泡泡中文字型：Noto Sans CJK TC Regular（SIL Open Font License）。
# 完整字型 16MB 放 unity/FontSource/（不在 Assets 底下，Unity 不會把它包進 build；不進 git），
# 再切出常用字子集到 unity/Assets/Resources/OfficeFont.otf（約 1.4MB，也不進 git）——見 tools/subset_font.py。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/unity/FontSource/NotoSansCJKtc-Regular.otf"
URL="https://github.com/notofonts/noto-cjk/releases/download/Sans2.004/09_NotoSansCJKtc.zip"

if [ ! -f "$SRC" ]; then
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "下載 Noto Sans CJK TC…（約 95MB 壓縮檔）"
  curl -sL -o "$TMP/noto.zip" "$URL"
  unzip -o -q "$TMP/noto.zip" NotoSansCJKtc-Regular.otf -d "$TMP"
  mkdir -p "$(dirname "$SRC")"
  cp "$TMP/NotoSansCJKtc-Regular.otf" "$SRC"
fi
uv run --no-project --with fonttools python "$ROOT/tools/subset_font.py" "$SRC"
echo "接著在 Unity 等它匯入完成，再 Tools → Build WebGL"

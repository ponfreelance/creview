#!/bin/bash
# ============================================
# CReview ビルドスクリプト (Mac / Linux)
# ============================================
set -e

echo ""
echo "========================================"
echo " CReview ビルド"
echo "========================================"
echo ""

# OS判定
case "$(uname -s)" in
    Darwin*)  OS_NAME="mac";;
    Linux*)   OS_NAME="linux";;
    *)        OS_NAME="unknown";;
esac

echo "[1/4] PyInstaller ビルド (${OS_NAME})..."
pyinstaller --onefile --name creview creview.py

echo ""
echo "[2/4] 配布ディレクトリ作成..."
rm -rf release
mkdir -p release
cp dist/creview release/
cp config.txt.sample release/
cp manual.txt release/
chmod +x release/creview

echo ""
echo "[3/4] SHA256 生成..."
if command -v sha256sum &> /dev/null; then
    sha256sum release/creview > release/sha256.txt
elif command -v shasum &> /dev/null; then
    shasum -a 256 release/creview > release/sha256.txt
else
    echo "SHA256ツールなし。スキップ。"
fi

echo ""
echo "[4/4] tar.gz 作成..."
ARCHIVE="creview_${OS_NAME}.tar.gz"
tar -czf "${ARCHIVE}" -C release .

echo ""
echo "========================================"
echo " 完了: ${ARCHIVE}"
echo "========================================"
echo ""
echo "GitHub Releaseにアップロードしてください"

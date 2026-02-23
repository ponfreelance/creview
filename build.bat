@echo off
echo.
echo  ========================================
echo   CReview ビルドスクリプト
echo  ========================================
echo.

echo [1/4] PyInstaller ビルド...
pyinstaller --onefile --name creview creview.py
if errorlevel 1 (
    echo ビルド失敗
    pause
    exit /b 1
)

echo.
echo [2/4] 配布ディレクトリ作成...
if exist release rmdir /s /q release
mkdir release
copy dist\creview.exe release\
copy config.txt.sample release\
copy manual.txt release\
copy start.bat release\
copy start_spec.bat release\

echo.
echo [3/4] SHA256 生成...
certutil -hashfile release\creview.exe SHA256 > release\sha256.txt 2>&1

echo.
echo [4/4] ZIP 作成...
powershell -Command "Compress-Archive -Path 'release\*' -DestinationPath 'creview_win.zip' -Force"

echo.
echo ========================================
echo  完了: creview_win.zip
echo ========================================
echo.
echo GitHub Releaseにアップロードしてください
pause

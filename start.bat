@echo off
chcp 65001 >nul 2>&1
echo.
echo  ========================================
echo   CReview - C設計レビューAI
echo  ========================================
echo.

if "%~1" NEQ "" (
    set "target=%~1"
    goto run
)

set /p "target=レビュー対象ファイルまたはフォルダを入力: "

if "%target%"=="" (
    echo 対象が指定されていません
    pause
    exit /b 1
)

:run
echo.
echo レビュー実行中...
echo.

"%~dp0creview.exe" %target%

echo.
echo ========================================
echo  完了
echo ========================================
pause

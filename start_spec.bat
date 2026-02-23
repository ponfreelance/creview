@echo off
chcp 65001 >nul 2>&1
echo.
echo  ========================================
echo   CReview - 仕様レビュー
echo  ========================================
echo.

if "%~1" NEQ "" (
    set "target=%~1"
    goto run
)

set /p "target=仕様ファイルを入力: "

if "%target%"=="" (
    echo 対象が指定されていません
    pause
    exit /b 1
)

:run
echo.
echo 仕様レビュー実行中...
echo.

"%~dp0creview.exe" --spec %target%

echo.
echo ========================================
echo  完了
echo ========================================
pause

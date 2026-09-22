@echo off
chcp 65001 > nul
echo [BD2 Controller] 빌드를 시작합니다...
python build_release.py
if errorlevel 1 (
    echo [오류] 빌드 중 오류가 발생했습니다.
    pause
    exit /b 1
)
pause


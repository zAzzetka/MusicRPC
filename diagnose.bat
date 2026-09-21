@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Сначала запустите install.bat
    pause
    exit /b 1
)
echo Перед проверкой закройте программу в трее: правый клик по значку - Выход.
echo.
".venv\Scripts\python.exe" music_rpc.pyw --diagnose
echo.
pause

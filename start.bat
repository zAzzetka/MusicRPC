@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    chcp 65001 >nul
    echo Сначала запустите install.bat
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "music_rpc.pyw"

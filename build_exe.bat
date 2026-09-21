@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Необязательно: собирает один файл MusicRPC.exe (папка dist) с иконкой из assets\app.ico.
if not exist ".venv\Scripts\python.exe" (
    echo Сначала запустите install.bat
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole --name MusicRPC ^
  --icon "assets\app.ico" ^
  --hidden-import pystray._win32 ^
  --hidden-import paho.mqtt.client ^
  --hidden-import winrt.windows.media.control ^
  --hidden-import winrt.windows.media ^
  --hidden-import winrt.windows.foundation ^
  --hidden-import winrt.windows.foundation.collections ^
  --hidden-import winrt.windows.storage.streams ^
  music_rpc.pyw
echo.
echo Готово: dist\MusicRPC.exe
echo Если у файла всё ещё старая иконка - это кэш значков Windows: переименуйте файл или перезапустите Проводник.
pause

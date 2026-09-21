@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка Music RPC ===
echo.

rem Music RPC работает на Python 3.10-3.14: для 3.15 и новее у пакетов winrt пока нет готовых сборок.
rem Какой бы Python ни стоял в системе, скрипт справится сам:
rem   1. подходящий Python есть - используем его;
rem   2. подходящего нет - скачиваем свой Python 3.13 в папку .tools, системный Python не затрагивается.

set "PY="

rem --- Окружение .venv уже есть и подходит? ---
if not exist ".venv\Scripts\python.exe" goto find_python
".venv\Scripts\python.exe" -c "import sys, tkinter; sys.exit(0 if sys.version_info[0] == 3 and 10 <= sys.version_info[1] <= 14 else 1)" >nul 2>nul
if not errorlevel 1 goto install_deps
echo Окружение .venv создано неподходящей версией Python, создаю заново...
rmdir /s /q ".venv"

:find_python
rem --- Ищем подходящий Python среди установленных ---
where py >nul 2>nul
if errorlevel 1 goto find_python_exe
for %%V in (3.13 3.12 3.14 3.11 3.10) do call :try_py "py -%%V"
if defined PY goto make_venv

:find_python_exe
where python >nul 2>nul
if errorlevel 1 goto need_uv
call :try_py "python"
if defined PY goto make_venv
goto need_uv

:make_venv
echo Используется системный Python:
%PY% --version
echo.
%PY% -m venv .venv
if errorlevel 1 goto venv_failed
goto install_deps

:need_uv
echo В системе нет подходящего Python 3.10-3.14 с tkinter.
echo Скачиваю свой Python 3.13 в папку .tools. Системный Python не затрагивается.
echo Нужен интернет, около 50 МБ.
echo.
set "UV_DIR=%~dp0.tools\uv"
set "UV_PYTHON_INSTALL_DIR=%~dp0.tools\python"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_ARCH=x86_64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "UV_ARCH=aarch64"
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 set "UV_ARCH=i686"
if exist "%UV_DIR%\uv.exe" goto have_uv
if not exist ".tools" mkdir ".tools"
echo Скачиваю uv - небольшой загрузчик Python...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-%UV_ARCH%-pc-windows-msvc.zip' -OutFile '.tools\uv.zip'; Expand-Archive -Force -Path '.tools\uv.zip' -DestinationPath '%UV_DIR%'; Remove-Item '.tools\uv.zip'"
if errorlevel 1 goto uv_failed
if not exist "%UV_DIR%\uv.exe" goto uv_failed

:have_uv
echo Скачиваю Python 3.13 и создаю окружение...
"%UV_DIR%\uv.exe" venv --python 3.13 --seed ".venv"
if errorlevel 1 goto uv_failed
goto install_deps

:install_deps
echo.
echo Устанавливаю зависимости...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto deps_failed
echo.
echo Готово! Теперь запускайте start.bat
echo.
pause
exit /b 0

:venv_failed
echo.
echo Не удалось создать окружение .venv
pause
exit /b 1

:uv_failed
echo.
echo Не удалось скачать Python автоматически. Проверьте интернет и запустите install.bat снова.
echo Если не помогает, установите Python 3.13 вручную: https://www.python.org/downloads/windows/
echo При установке отметьте галочку "Add python.exe to PATH".
pause
exit /b 1

:deps_failed
echo.
echo Не удалось установить зависимости. Проверьте интернет и запустите install.bat снова.
echo Если не помогает, удалите папки .venv и .tools и запустите install.bat ещё раз.
pause
exit /b 1

:try_py
rem Подпрограмма: если Python из аргумента подходит (3.10-3.14 и есть tkinter), запоминаем его в PY.
if defined PY goto :eof
%~1 -c "import sys, tkinter; sys.exit(0 if sys.version_info[0] == 3 and 10 <= sys.version_info[1] <= 14 else 1)" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
goto :eof

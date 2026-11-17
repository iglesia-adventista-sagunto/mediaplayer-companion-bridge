@echo off
setlocal
cd /d "%~dp0"

set "APP_LOCAL=%LOCALAPPDATA%\mediaplayer-companion-bridge"
set "VENV_DIR=%APP_LOCAL%\venv"
if not defined CACHE_DIR set "CACHE_DIR=%APP_LOCAL%\cache"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating machine-local virtual environment at "%VENV_DIR%"...
    if not exist "%APP_LOCAL%" mkdir "%APP_LOCAL%"
    python -m venv "%VENV_DIR%"
    if errorlevel 1 exit /b 1
    "%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
    "%VENV_DIR%\Scripts\pip.exe" install -r requirements.txt
    if errorlevel 1 exit /b 1
)

if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%"

echo Starting MediaPlayer Companion Bridge (port from config.json / PORT env, default 5002)...
"%VENV_DIR%\Scripts\python.exe" app.py %*

@echo off
setlocal
cd /d "%~dp0"

set "APP_LOCAL=%LOCALAPPDATA%\mediaplayer-companion-bridge"
set "VENV_DIR=%APP_LOCAL%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not defined CACHE_DIR set "CACHE_DIR=%APP_LOCAL%\cache"

if /i "%~1"=="setup" (
    shift
    if exist "%VENV_PY%" (
        "%VENV_PY%" setup_wizard.py %1 %2 %3 %4 %5 %6 %7 %8 %9
    ) else (
        python setup_wizard.py %1 %2 %3 %4 %5 %6 %7 %8 %9
    )
    exit /b %ERRORLEVEL%
)

if /i "%~1"=="install" (
    shift
    if exist "%VENV_PY%" (
        "%VENV_PY%" setup_wizard.py --install-only %1 %2 %3 %4 %5 %6 %7 %8 %9
    ) else (
        python setup_wizard.py --install-only %1 %2 %3 %4 %5 %6 %7 %8 %9
    )
    exit /b %ERRORLEVEL%
)

if /i "%~1"=="uninstall" (
    shift
    if exist "%VENV_PY%" (
        "%VENV_PY%" setup_wizard.py --uninstall %1 %2 %3 %4 %5 %6 %7 %8 %9
    ) else (
        python setup_wizard.py --uninstall %1 %2 %3 %4 %5 %6 %7 %8 %9
    )
    exit /b %ERRORLEVEL%
)

if not exist "%VENV_PY%" (
    echo Creating machine-local virtual environment at "%VENV_DIR%"...
    if not exist "%APP_LOCAL%" mkdir "%APP_LOCAL%"
    python -m venv "%VENV_DIR%"
    if errorlevel 1 exit /b 1
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_DIR%\Scripts\pip.exe" install -r requirements.txt
    if errorlevel 1 exit /b 1
)

if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%"
if not exist "config.json" if exist "config.template.json" (
    copy /y "config.template.json" "config.json" >nul
    echo Created config.json from config.template.json
)

echo Starting MediaPlayer Companion Bridge...
"%VENV_PY%" app.py %*

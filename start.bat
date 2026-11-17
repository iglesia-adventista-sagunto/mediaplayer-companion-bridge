@echo off
setlocal
pushd "%~dp0" || exit /b 1

set "APP_LOCAL=%LOCALAPPDATA%\mediaplayer-companion-bridge"
set "VENV_DIR=%APP_LOCAL%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not defined CACHE_DIR set "CACHE_DIR=%APP_LOCAL%\cache"

if /i "%~1"=="help" goto :show_help
if /i "%~1"=="--help" goto :show_help
if /i "%~1"=="-h" goto :show_help
if "%~1"=="/?" goto :show_help

if /i "%~1"=="setup" (
    shift
    call :run_wizard %1 %2 %3 %4 %5 %6 %7 %8 %9
    set "EXIT_CODE=%ERRORLEVEL%"
    popd
    exit /b %EXIT_CODE%
)

if /i "%~1"=="install" (
    shift
    call :run_wizard --install-only %1 %2 %3 %4 %5 %6 %7 %8 %9
    set "EXIT_CODE=%ERRORLEVEL%"
    popd
    exit /b %EXIT_CODE%
)

if /i "%~1"=="uninstall" (
    shift
    call :run_wizard --uninstall %1 %2 %3 %4 %5 %6 %7 %8 %9
    set "EXIT_CODE=%ERRORLEVEL%"
    popd
    exit /b %EXIT_CODE%
)

call :ensure_venv
if errorlevel 1 (
    popd
    exit /b 1
)

if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%"
if not exist "config.json" if exist "config.template.json" (
    copy /y "config.template.json" "config.json" >nul
    echo Created config.json from config.template.json
)

echo Starting MediaPlayer Companion Bridge...
"%VENV_PY%" app.py %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

:show_help
echo Usage: start.bat [command] [options]
echo.
echo Commands:
echo   (none)      Create/repair venv, initialize config.json, and start the bridge
echo   setup       Run the interactive setup wizard (pass --non-interactive, --install, etc.)
echo   install     Install/update venv and register Windows logon auto-start task
echo   uninstall   Stop running bridge processes and remove Windows logon auto-start task
echo   --no-tray   Start headless without the system tray icon
popd
exit /b 0

:run_wizard
if exist "%VENV_PY%" (
    "%VENV_PY%" setup_wizard.py %*
    exit /b %ERRORLEVEL%
)
call :find_python
if errorlevel 1 exit /b 1
"%SYS_PY%" setup_wizard.py %*
exit /b %ERRORLEVEL%

:ensure_venv
if not exist "%VENV_PY%" (
    call :find_python
    if errorlevel 1 exit /b 1
    echo Creating machine-local virtual environment at "%VENV_DIR%"...
    if not exist "%APP_LOCAL%" mkdir "%APP_LOCAL%"
    "%SYS_PY%" -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment at "%VENV_DIR%".
        exit /b 1
    )
)

"%VENV_PY%" -c "import flask, PIL, pystray, requests" >nul 2>&1
if errorlevel 1 (
    echo Installing Python dependencies into "%VENV_DIR%"...
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies from requirements.txt.
        exit /b 1
    )
)
exit /b 0

:find_python
set "SYS_PY="

for /f "delims=" %%I in ('where python 2^>nul') do (
    echo "%%I" | findstr /i /c:"\Microsoft\WindowsApps\" >nul
    if errorlevel 1 (
        "%%I" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
        if not errorlevel 1 (
            set "SYS_PY=%%I"
            exit /b 0
        )
    )
)

where py >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2^>nul') do (
        if not errorlevel 1 (
            set "SYS_PY=%%I"
            exit /b 0
        )
    )
)

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
    if exist "%%~fD\python.exe" (
        "%%~fD\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
        if not errorlevel 1 (
            set "SYS_PY=%%~fD\python.exe"
            exit /b 0
        )
    )
)

echo [ERROR] Python 3.10 or newer was not found.
echo Install Python 3.12+ from https://www.python.org/downloads/ (check "Add python.exe to PATH")
echo or run: winget install Python.Python.3.12
exit /b 1

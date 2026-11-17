@echo off
setlocal
cd /d "%~dp0"

set "VENV_PY=%LOCALAPPDATA%\mediaplayer-companion-bridge\venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    "%VENV_PY%" setup_wizard.py %*
) else (
    python setup_wizard.py %*
)
exit /b %ERRORLEVEL%

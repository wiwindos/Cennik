@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if not defined CENNIK_OWNER_KEY (
    echo Set CENNIK_OWNER_KEY to a private random key of at least 32 characters.
    pause
    exit /b 1
)
"venv\Scripts\python.exe" -m cennik.server %*
if errorlevel 1 pause

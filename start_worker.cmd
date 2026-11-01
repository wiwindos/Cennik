@echo off
setlocal
cd /d "%~dp0"
if exist "dist\CennikWorker.exe" (
    start "" "%~dp0dist\CennikWorker.exe" %*
    exit /b 0
)
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
start "" "%~dp0venv\Scripts\pythonw.exe" -m cennik.worker.desktop %*

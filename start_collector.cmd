@echo off
setlocal
cd /d "%~dp0"
if exist "dist\CennikCollector.exe" (
    start "" "%~dp0dist\CennikCollector.exe" %*
    exit /b 0
)
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if not exist "venv\Scripts\pythonw.exe" (
    echo Build dist\CennikCollector.exe or install the collector dependencies from README.md.
    pause
    exit /b 1
)
start "" "%~dp0venv\Scripts\pythonw.exe" -m cennik.collector.desktop %*

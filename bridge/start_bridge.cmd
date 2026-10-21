@echo off
cd /d "%~dp0.."
venv\Scripts\python.exe -m cennik.addon.bridge %*
if errorlevel 1 pause

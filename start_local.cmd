@echo off
cd /d "%~dp0"
venv\Scripts\python.exe -m ahledger local
if errorlevel 1 pause

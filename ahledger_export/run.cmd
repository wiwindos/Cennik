@echo off
setlocal
if exist "%~dp0..\venv\Scripts\python.exe" (
    "%~dp0..\venv\Scripts\python.exe" "%~dp0export.py" %*
) else (
    py -3 "%~dp0export.py" %*
)
set "exportExitCode=%errorlevel%"
echo.
echo Export exit code: %exportExitCode%
pause
exit /b %exportExitCode%

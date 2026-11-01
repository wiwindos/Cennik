@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

"venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1
if not errorlevel 1 goto run

rem Repair a copied Python 3.12 environment without replacing installed packages.
if not defined CENNIK_PYTHON set "CENNIK_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%CENNIK_PYTHON%" goto python_missing
"%CENNIK_PYTHON%" -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>&1
if errorlevel 1 goto python_missing
if not exist "venv\pyvenv.cfg" goto python_missing
echo Repairing the virtual environment with "%CENNIK_PYTHON%"...
"%CENNIK_PYTHON%" -m venv --upgrade --without-pip venv
if errorlevel 1 goto failed

:run
"venv\Scripts\python.exe" -m cennik.workspace %*
set "CENNIK_EXIT_CODE=%ERRORLEVEL%"
if not "%CENNIK_EXIT_CODE%"=="0" pause
exit /b %CENNIK_EXIT_CODE%

:python_missing
echo Cennik needs a working Python environment. See README.md for first-time setup.
echo To repair an existing Python 3.12 environment, set CENNIK_PYTHON to the full path of python.exe.
:failed
pause
exit /b 1

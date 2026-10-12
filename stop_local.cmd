@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "$taskRoot = (Resolve-Path -LiteralPath 'data').Path; $taskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/healthz' -TimeoutSec 3; if (-not $taskHealth.local_mode -or $taskHealth.workspace -ne $taskRoot) { throw 'This port belongs to another app' }; $taskListener = Get-NetTCPConnection -LocalPort 8765 -State Listen | Where-Object LocalAddress -eq '127.0.0.1' | Select-Object -First 1; $taskProcess = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $taskListener.OwningProcess); if ($taskProcess.CommandLine -notmatch '-m\s+ahledger\s+local(?:\s|$)') { throw 'Unexpected process; app was not stopped' }; Stop-Process -Id $taskProcess.ProcessId"
if errorlevel 1 pause

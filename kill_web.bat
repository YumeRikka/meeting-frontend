@echo off
REM Kill any stale meeting-card backend before (re)starting.
REM No third-party deps: uses netstat + taskkill + PowerShell (both built into Windows).
echo Stopping any stale backend on port 5000 ...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /i "LISTENING" ^| findstr /i ":5000"') do (
    echo   killing PID %%a (port 5000)
    taskkill /PID %%a /F >nul 2>&1
)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*app.py*' } | ForEach-Object { Write-Host ('  killing PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Done.

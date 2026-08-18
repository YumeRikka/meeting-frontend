@echo off
REM One-click stop for the meeting-card backend.
cd /d "%~dp0"
call kill_web.bat
echo Backend stopped. You can close this window.
pause

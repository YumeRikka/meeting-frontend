@echo off
REM Start Cloudflare Tunnel: expose api.rikka.com.cn to localhost:5000
REM Prereq: Flask backend must be running on port 5000 (use run_web.bat first)
REM Keep this window open = tunnel running; close window = tunnel stopped.
title Cloudflare Tunnel (meeting-api)
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel run meeting-api
pause

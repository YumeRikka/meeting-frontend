@echo off
REM 启动 Cloudflare Tunnel：将 api.rikka.com.cn 暴露到本机 localhost:5000
REM 前提：本机 Flask 后端已在 5000 端口运行（用 run_web.bat 启动）
REM 保持此窗口打开 = 隧道运行中；关闭窗口 = 停止隧道。
title Cloudflare Tunnel (meeting-api)
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel run meeting-api
pause

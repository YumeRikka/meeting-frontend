@echo off
REM 会议卡密后端启动脚本（强制使用项目 .venv，杜绝裸跑系统 Python）
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt -q
python h5\app.py
if errorlevel 1 pause

@echo off
REM Meeting-card backend startup (forces project .venv, kills stale instance first)
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt -q
call kill_web.bat
python h5\app.py
if errorlevel 1 pause

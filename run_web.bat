@echo off
REM Meeting-card backend startup (forces project .venv, avoids bare system Python)
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt -q
python h5\app.py
if errorlevel 1 pause

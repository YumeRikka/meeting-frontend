@echo off
REM Meeting-card backend regression suite (zero-dependency, Python built-in unittest)
REM Usage: double-click this file, or run run_tests.bat from cmd
cd /d %~dp0
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] .venv not found. Create the Python 3.13 venv and install requirements.txt first.
    pause
    exit /b 1
)
python -m unittest discover -s tests -p "test_*.py" -v
pause

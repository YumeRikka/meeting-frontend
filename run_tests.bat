@echo off
REM 会议卡密后端 · 回归测试套件（零依赖，使用 Python 内置 unittest）
REM 用法：双击本文件，或 cmd 中执行 run_tests.bat
cd /d %~dp0
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [错误] 未找到 .venv，请先创建 Python 3.13 虚拟环境并安装 requirements.txt
    pause
    exit /b 1
)
python -m unittest discover -s tests -p "test_*.py" -v
pause

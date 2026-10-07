@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 创建虚拟环境并安装依赖……
    python -m venv .venv || goto :err
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
) else (
    echo [1/2] 已存在虚拟环境，跳过安装
)

echo [2/2] 启动 Web 控制台（浏览器打开 http://127.0.0.1:8787/）……
echo      旧版 Tkinter 窗口可以用：.venv\Scripts\python.exe -m hd2coyote ui
".venv\Scripts\python.exe" -m hd2coyote web
goto :eof

:err
echo.
echo 安装失败。请确认已安装 Python 3.10+ 并加入 PATH：https://www.python.org/downloads/
pause

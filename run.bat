@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

rem Start the hd2-coyote Web console (browser UI at http://127.0.0.1:8787/).
rem To quit: click "Close program" in the page, or run stop.bat, or press Ctrl+C here.

set PY=
if exist "..\.venv\Scripts\python.exe" (
    echo [1/2] Reusing the virtual environment in the parent folder.
    set PY=..\.venv\Scripts\python.exe
) else if exist ".venv\Scripts\python.exe" (
    echo [1/2] Reusing .venv in this folder.
    set PY=.venv\Scripts\python.exe
) else (
    echo [1/2] Creating a virtual environment and installing dependencies...
    python -m venv .venv || goto :err
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
    set PY=.venv\Scripts\python.exe
)

echo [2/2] Starting the Web console...
echo       The old Tkinter window is still available: "%PY%" -m hd2coyote ui
"%PY%" -m hd2coyote web
goto :eof

:err
echo.
echo Install failed. Please install Python 3.10+ and add it to PATH:
echo https://www.python.org/downloads/
pause

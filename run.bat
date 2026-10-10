@echo off
setlocal
cd /d "%~dp0"
if exist "dist\HD2-DG-LAB.exe" (
    start "" "dist\HD2-DG-LAB.exe"
    exit /b 0
)
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
if not defined PY (
    py -3 -m venv .venv
    if errorlevel 1 goto fail
    set "PY=.venv\Scripts\python.exe"
)
"%PY%" -c "import webview, clr, websockets, qrcode" >nul 2>&1
if errorlevel 1 (
    "%PY%" -m pip install -r requirements-desktop.txt
    if errorlevel 1 goto fail
)
"%PY%" -m hd2coyote.desktop
if errorlevel 1 goto fail
exit /b 0
:fail
echo Desktop startup failed. See docs\DESKTOP.md for setup and Web fallback.
pause
exit /b 1

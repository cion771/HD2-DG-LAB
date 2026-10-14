@echo off
setlocal
cd /d "%~dp0"
set "VERSION="
if exist "VERSION" set /p VERSION=<"VERSION"
if defined VERSION if exist "dist\HD2-DG-LAB-%VERSION%.exe" (
    start "" "dist\HD2-DG-LAB-%VERSION%.exe"
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
echo Desktop startup failed. See docs\DESKTOP.md and install Microsoft Edge WebView2 Runtime.
pause
exit /b 1

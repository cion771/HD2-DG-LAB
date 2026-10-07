@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

rem Close the hd2-coyote controller (Web console).
rem Usage: stop.bat [port]        (default 8787)
rem
rem It first asks the controller to exit gracefully (output to zero, waveforms
rem cleared, device disconnected, then the server shuts down) and only kills the
rem process by port if that fails. The logic lives in hd2coyote/console_ctl.py.
rem
rem Note: `stop` needs neither numpy nor websockets, so any Python 3.10+ works.

set PORT=%1
if "%PORT%"=="" set PORT=8787

set PY=
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY if exist "..\.venv\Scripts\python.exe" set PY=..\.venv\Scripts\python.exe
if not defined PY set PY=python

echo Using: %PY%
"%PY%" -m hd2coyote stop --port %PORT%

echo.
pause

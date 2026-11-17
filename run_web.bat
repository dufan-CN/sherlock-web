@echo off
rem Sherlock Web launcher: restart on crash, log to service.log
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
rem Listen on all interfaces so LAN machines can reach it (public access still needs a tunnel/reverse proxy)
if not defined SHERLOCK_WEB_HOST set "SHERLOCK_WEB_HOST=0.0.0.0"
set "PY=python"
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if defined SHERLOCK_PYTHON set "PY=%SHERLOCK_PYTHON%"
:loop
"%PY%" "%~dp0sherlock_web.py" >> "%~dp0service.log" 2>&1
timeout /t 5 /nobreak >nul
goto loop

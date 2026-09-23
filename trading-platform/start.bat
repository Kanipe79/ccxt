@echo off
REM One command from a fresh clone to a running dashboard (Windows).
REM   start.bat          dashboard; launch the bot from the browser
REM   start.bat demo     dashboard + a demo run started for you
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\ux.exe (
  echo ux-trader: first run - creating .venv and installing...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\pip install -q -e ..
  .venv\Scripts\pip install -q -e .
)
.venv\Scripts\ux.exe %*

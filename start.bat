@echo off
REM ============================================================
REM  AiChat SSF - one-click launcher (Windows, no Docker).
REM
REM  Messages here are ASCII ONLY on purpose: a .bat saved as UTF-8
REM  with Cyrillic text breaks cmd.exe (it reads .bat in the OEM code
REM  page and tries to run the garbled text as commands). The web UI
REM  and the app itself stay in Russian - only this launcher is ASCII.
REM
REM  What it does: create .venv, install deps, pick a FREE port (8000 is
REM  often busy), wait until the server answers, then open the browser.
REM ============================================================
setlocal enableextensions
cd /d "%~dp0"
title AiChat SSF launcher

echo ============================================
echo   AiChat SSF
echo ============================================

REM 1) Create .env from template on first run.
if not exist ".env" (
  echo [setup] Creating .env from .env.example
  copy ".env.example" ".env" >nul
)

REM 2) Virtual environment (.venv), prefer Python 3.12.
if not exist ".venv\Scripts\python.exe" (
  echo [setup] Creating .venv ...
  py -3.12 -m venv .venv 2>nul || py -m venv .venv 2>nul || python -m venv .venv
)
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [error] Python not found. Install Python 3.10+ from python.org and retry.
  pause
  exit /b 1
)

REM 3) Dependencies - install once (marker file .venv\.installed).
if not exist ".venv\.installed" (
  echo [setup] Installing dependencies. First time takes a couple of minutes...
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install -r backend\requirements.txt
  if errorlevel 1 (
    echo [error] Dependency install failed. Check internet and retry.
    pause
    exit /b 1
  )
  echo installed> ".venv\.installed"
)

REM 4) Pick a free port via a small Python helper (avoids fragile batch parsing).
set "PORT="
for /f "usebackq tokens=*" %%P in (`"%PY%" scripts\free_port.py`) do set "PORT=%%P"
if not defined PORT (
  echo [error] No free port found in 8000..7860.
  pause
  exit /b 1
)
set "URL=http://127.0.0.1:%PORT%"
REM Detect LAN IP so other devices (phone, another PC) can connect.
set "LANIP=127.0.0.1"
for /f "usebackq tokens=*" %%I in (`"%PY%" scripts\lan_ip.py`) do set "LANIP=%%I"
echo [run] Local URL:   %URL%
echo [run] Network URL: http://%LANIP%:%PORT%   (open on other LAN devices)

REM 5) Start the server in its own window, bound to 0.0.0.0 so it is reachable
REM    over the local network. "cmd /k" keeps the window OPEN on error.
REM    NOTE: Windows Firewall may pop up the first time - click "Allow access".
REM    Launch via run.py (NOT plain uvicorn): it sets ws_max_size=None so there is
REM    NO WebSocket message size limit (big audio/photo attachments go through).
start "AiChat backend" cmd /k ".venv\Scripts\python.exe run.py --host 0.0.0.0 --port %PORT%"

REM 6) The Telegram bot is now started from the in-app Admin panel
REM    (set the token there and press Start). No separate process here, so two
REM    pollers never fight over the same bot.

REM 7) Wait until the server actually answers, then open the browser.
echo [wait] Waiting for the server (first LiteLLM import is slow)...
for /l %%i in (1,1,60) do (
  "%PY%" -c "import urllib.request; urllib.request.urlopen('%URL%/api/health',timeout=1)" >nul 2>&1 && (
    echo [ok] Server is up, opening browser
    start "" "%URL%"
    goto :launched
  )
  timeout /t 1 /nobreak >nul
)
echo [warn] Server did not answer in 60s. Open the "AiChat backend" window to see the error.
start "" "%URL%"

:launched
echo.
echo Done.  Local: %URL%   Network: http://%LANIP%:%PORT%
echo To stop: close the "AiChat backend" window (and the Telegram bot window).
echo (Changed dependencies? Delete .venv\.installed to reinstall.)
pause
endlocal

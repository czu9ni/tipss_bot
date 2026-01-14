@echo off
setlocal
cd /d "%~dp0"
if exist .env (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)
if not defined SOCCER_DB_URL (
  set "SOCCER_DB_URL=sqlite:///soccer.db"
)
if not defined ODDS_API_KEY (
  echo HIBA: ODDS_API_KEY nincs beallitva. Tedd a .env fajlba vagy add meg set paranccsal.
  pause
  exit /b 1
)
if not defined FOOTBALL_DATA_TOKEN (
  echo HIBA: FOOTBALL_DATA_TOKEN nincs beallitva. Tedd a .env fajlba vagy add meg set paranccsal.
  pause
  exit /b 1
)
start "" /b python web.py
timeout /t 2 >nul
start "" http://127.0.0.1:5000

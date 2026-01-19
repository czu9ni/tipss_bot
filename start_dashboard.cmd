@echo off
cd /d "%~dp0"

REM indítsuk el a Flask szervert háttérben
start "" "%~dp0run_web.cmd"

REM várjunk pár másodpercet, hogy a szerver felálljon
timeout /t 3 /nobreak >nul

REM nyissuk meg a böngészőt a friss dashboarddal
start "" "http://127.0.0.1:5000"

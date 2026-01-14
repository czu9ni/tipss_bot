@echo off
setlocal
cd /d "%~dp0"

if exist .env (
  for /f "usebackq tokens=1* delims== eol=#" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

if "%SOCCER_DB_URL%"=="" set "SOCCER_DB_URL=sqlite:///soccer.db"

where python >nul 2>nul
if errorlevel 1 (
  set "PYTHON=py"
) else (
  set "PYTHON=python"
)

start "SoccerBot" cmd /c "%PYTHON% web.py"

timeout /t 1 /nobreak >nul
start "SoccerBot" http://127.0.0.1:5000/
endlocal

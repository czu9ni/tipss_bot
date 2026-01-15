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

:loop
%PYTHON% web.py
timeout /t 5 /nobreak >nul
goto loop

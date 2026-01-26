@echo off
setlocal
cd /d "%~dp0"

for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'web.py' } | Select-Object -ExpandProperty ProcessId"`) do (
  taskkill /F /PID %%P >nul 2>&1
)

if exist .env (
  for /f "usebackq tokens=1* delims== eol=#" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

if "%SOCCER_DB_URL%"=="" set "SOCCER_DB_URL=sqlite:///soccer.db"

if exist "%LocalAppData%\Python\pythoncore-3.14-64\python.exe" (
  set "PYTHON=%LocalAppData%\Python\pythoncore-3.14-64\python.exe"
) else if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
  set "PYTHON=%LocalAppData%\Programs\Python\Python311\python.exe"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    set "PYTHON=py"
  ) else (
    set "PYTHON=python"
  )
)

:loop
%PYTHON% web.py
timeout /t 5 /nobreak >nul
goto loop

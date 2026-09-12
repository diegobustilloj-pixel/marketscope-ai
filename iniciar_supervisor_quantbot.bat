@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo ERROR: falta .venv\Scripts\pythonw.exe
  exit /b 2
)

start "PolyMarker QuantBot Supervisor" /min ".venv\Scripts\pythonw.exe" "supervisor_quantbot.py" --monitor
timeout /t 2 /nobreak >nul
".venv\Scripts\python.exe" "supervisor_quantbot.py" --status
exit /b %errorlevel%

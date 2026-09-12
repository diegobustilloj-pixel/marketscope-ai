@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: falta .venv\Scripts\python.exe
  pause
  exit /b 2
)
".venv\Scripts\python.exe" "monitor_forward_acelerado_v094.py" --db "data\shadow_forward_twap_transfer_v093.db"
echo.
pause

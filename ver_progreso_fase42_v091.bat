@echo off
setlocal
cd /d "%~dp0"
set "SHADOW_DB=data\shadow_tecnica_fase42_v091.db"
if not exist "%SHADOW_DB%" (
  echo No existe %SHADOW_DB%
  pause
  exit /b 2
)
".venv\Scripts\python.exe" -m polymarket_bot shadow-status --shadow-db "%SHADOW_DB%"
pause

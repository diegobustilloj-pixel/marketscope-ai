@echo off
setlocal
cd /d "%~dp0"
set "SHADOW_DB=data\shadow_forward_fase41.db"

if not exist "%SHADOW_DB%" (
  echo La prueba shadow todavia no fue iniciada.
  pause
  exit /b 2
)

echo ============================================================
echo PROGRESO SHADOW FORWARD
echo ============================================================
.\.venv\Scripts\python.exe -m polymarket_bot shadow-status --shadow-db "%SHADOW_DB%"
echo.
echo Esta consulta no detuvo ni modifico la prueba.
pause

@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "estado_general.py"
echo.
pause

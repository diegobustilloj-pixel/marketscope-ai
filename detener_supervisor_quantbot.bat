@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "supervisor_quantbot.py" --stop
echo.
echo Los collectors y monitores NO fueron detenidos.
pause

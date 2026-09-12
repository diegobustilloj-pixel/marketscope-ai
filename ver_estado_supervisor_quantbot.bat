@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "supervisor_quantbot.py" --status
echo.
pause

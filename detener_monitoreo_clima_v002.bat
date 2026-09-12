@echo off
setlocal
cd /d "%~dp0"
.venv\Scripts\python.exe climate_live_v002.py stop
pause

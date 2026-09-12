@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "climate_shadow_monitor_v001.py" stop --database "data\climate_shadow_forward_v001.db"
endlocal

@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "elon_shadow_monitor_v001.py" stop --database "data\elon_shadow_forward_v001.db"
echo Se solicito una detencion limpia al finalizar el ciclo actual.
endlocal

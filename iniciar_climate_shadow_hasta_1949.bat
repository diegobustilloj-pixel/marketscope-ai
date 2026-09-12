@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: no se encontro .venv\Scripts\python.exe
  exit /b 1
)
start "Climate Shadow V001" /min ".venv\Scripts\python.exe" "climate_shadow_monitor_v001.py" run --database "data\climate_shadow_forward_v001.db" --research-root "data\climate_research_v001" --end-at "2026-09-02T23:49:18Z" --poll-seconds 600 --forecast-refresh-seconds 3600 --workers 8
echo Collector climatico shadow iniciado. No usa wallet ni envia ordenes.
endlocal

@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: no se encontro .venv\Scripts\python.exe
  exit /b 1
)
start "Elon Shadow V001" /min ".venv\Scripts\python.exe" "elon_shadow_monitor_v001.py" run --database "data\elon_shadow_forward_v001.db" --research-root "data\elon_post_count_v001" --target-hours 24 --poll-seconds 30
echo Monitor shadow iniciado. No usa wallet ni envia ordenes.
endlocal

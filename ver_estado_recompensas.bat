@echo off
setlocal
cd /d "%~dp0"
if exist "data\reward_mm_shadow_v001\latest_summary.json" (
  type "data\reward_mm_shadow_v001\latest_summary.json"
) else (
  echo Aun no existe un resultado. Ejecute actualizar_scanner_recompensas.bat
)
echo.
pause

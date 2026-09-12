@echo off
setlocal
cd /d "%~dp0"
.venv\Scripts\python.exe climate_live_v002.py once
if errorlevel 1 goto :error
start "" "data\climate_live_v002\SENAL_CLIMA_LATEST.md"
exit /b 0

:error
echo No se pudo actualizar la señal climática.
pause
exit /b 1

@echo off
setlocal
cd /d "%~dp0"
set PM_DB_PATH=data\polymarket_phase1_v4.db
set PM_LOG_LEVEL=INFO
echo Configuracion temporal aplicada a esta ventana.
echo Use: .\.venv\Scripts\python.exe -m polymarket_bot demo
cmd /k

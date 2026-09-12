@echo off
setlocal
cd /d "%~dp0"
.venv\Scripts\python.exe climate_manual_strategy_v001.py --database data\climate_live_v002\climate_live_v002.db --output data\climate_live_v002\backtest --fetch-outcomes --net-edge-min 0.05 --require-entry-fee-schedule --preregistration docs\PREREG_CLIMATE_LIVE_V002_20260903.md
pause

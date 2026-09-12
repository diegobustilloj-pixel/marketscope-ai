@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe climate_realistic_backtest_v003.py
if errorlevel 1 pause & exit /b 1
start "" "data\climate_realistic_backtest_v003\BACKTEST_AUTOMATICO_REALISTA.md"

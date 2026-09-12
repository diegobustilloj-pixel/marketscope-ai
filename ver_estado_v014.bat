@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo ESTADO V0.14 - DESARROLLO DISJUNTO PAPER ONLY
echo ============================================================
echo.
.\.venv\Scripts\python.exe v014_monitor.py --status
echo.
.\.venv\Scripts\python.exe paper_trader_v014.py --status
echo.
echo Logs:
echo   data\v014_monitor.log
echo   data\v014_monitor.err.log
echo   data\v014_paper.log
echo   data\v014_paper.err.log
echo.
echo Dinero real: BLOQUEADO
endlocal

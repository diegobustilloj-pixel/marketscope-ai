@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo PRUEBA OFICIAL RTDS TWAP - BTC 30s
echo ============================================================
echo.
echo Esta prueba:
echo - dura 90 segundos;
echo - usa el topic oficial crypto_prices_twap_thirty;
echo - compara tambien el Chainlink spot existente;
echo - NO usa wallet, claves ni dinero real.
echo.

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: no se encontro .venv\Scripts\python.exe
  pause
  exit /b 2
)

".venv\Scripts\python.exe" probar_rtds_twap_oficial.py
set "CODE=%errorlevel%"

echo.
echo Al terminar, envie:
echo data\rtds_probe_twap_oficial.jsonl
echo.
pause
exit /b %CODE%

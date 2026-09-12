@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo No se encontro .venv\Scripts\python.exe en esta carpeta.
  echo Copie este archivo y probar_rtds_twap.py a la raiz de polymarket_quant_bot.
  pause
  exit /b 2
)
.\.venv\Scripts\python.exe probar_rtds_twap.py
set "CODE=%errorlevel%"
echo.
if "%CODE%"=="0" (
  echo Probe RTDS finalizado correctamente.
  echo Envie data\rtds_probe_twap.jsonl para revision.
) else (
  echo Probe RTDS termino con codigo %CODE%.
)
pause
exit /b %CODE%

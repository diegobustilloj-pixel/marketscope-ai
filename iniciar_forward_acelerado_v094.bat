@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo FASE 4.2 v0.9.4 - FORWARD ACELERADO MAXIMO
echo ============================================================
echo.
echo 1) Inicia/reanuda el forward BTC TWAP v0.9.3 SIN CAMBIAR el modelo.
echo 2) Abre un monitor secuencial en esta ventana.
echo 3) Prueba thresholds 0.06/0.08/0.10/0.12/0.15 EN PAPER.
echo 4) Congela threshold con bloque futuro y confirma con datos posteriores.
echo 5) Dinero real permanece BLOQUEADO.
echo.

if not exist ".venv\Scripts\python.exe" goto :missing_python
if not exist "iniciar_forward_twap_transfer_7_dias_v093.bat" goto :missing_forward
if not exist "monitor_forward_acelerado_v094.py" goto :missing_monitor

echo Iniciando forward en una ventana separada...
start "FORWARD BTC TWAP v0.9.3" cmd /k "cd /d ""%~dp0"" && call iniciar_forward_twap_transfer_7_dias_v093.bat"

echo Esperando 10 segundos para que se cree/reabra la base...
timeout /t 10 /nobreak >nul

echo.
echo Iniciando monitor secuencial. Se actualiza cada 5 minutos.
echo Puede cerrar SOLO esta ventana de monitor; el forward seguira en la otra.
echo.
".venv\Scripts\python.exe" "monitor_forward_acelerado_v094.py" --db "data\shadow_forward_twap_transfer_v093.db" --watch --interval 300
pause
exit /b 0

:missing_python
echo ERROR: falta .venv\Scripts\python.exe
pause
exit /b 2

:missing_forward
echo ERROR: falta iniciar_forward_twap_transfer_7_dias_v093.bat
pause
exit /b 3

:missing_monitor
echo ERROR: falta monitor_forward_acelerado_v094.py
pause
exit /b 4

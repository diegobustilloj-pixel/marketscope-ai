@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo FASE 4.2 v0.9.4a1 - FORWARD ACELERADO + WATCHDOG
echo ============================================================
echo.
echo - Base NUEVA v094a; no mezcla el tramo RTDS congelado anterior.
echo - Watchdog TWAP reconecta RTDS tras 12 s sin TWAP valido.
echo - Monitor secuencial prueba edges 0.06/0.08/0.10/0.12/0.15 en paper.
echo - Dinero real BLOQUEADO.
echo.

if not exist ".venv\Scripts\python.exe" goto :missing_python
if not exist "iniciar_forward_twap_transfer_v094a_7_dias.bat" goto :missing_forward
if not exist "monitor_forward_acelerado_v094a.py" goto :missing_monitor

echo Iniciando forward limpio v094a en otra ventana...
start "FORWARD BTC TWAP WATCHDOG v0.9.4a1" cmd /k "cd /d ""%~dp0"" && call iniciar_forward_twap_transfer_v094a_7_dias.bat"

echo Esperando 10 segundos para crear la base...
timeout /t 10 /nobreak >nul

echo.
echo Iniciando monitor. Se actualiza cada 5 minutos.
echo Puede cerrar SOLO este monitor con Ctrl+C sin detener el forward.
echo.
".venv\Scripts\python.exe" "monitor_forward_acelerado_v094a.py" --db "data\shadow_forward_twap_transfer_v094a.db" --watch --interval 300
pause
exit /b 0

:missing_python
echo ERROR: falta .venv\Scripts\python.exe
pause
exit /b 2
:missing_forward
echo ERROR: falta iniciar_forward_twap_transfer_v094a_7_dias.bat
pause
exit /b 3
:missing_monitor
echo ERROR: falta monitor_forward_acelerado_v094a.py
pause
exit /b 4

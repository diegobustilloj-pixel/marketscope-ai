@echo off
setlocal
cd /d "%~dp0"

set "PHASE4_DB=data\fase4_modelos.db"
set "MODEL_FILE=data\modelos_twap_transfer_v093.joblib"
set "SHADOW_DB=data\shadow_forward_twap_transfer_v094a.db"
set "AUDIT_RESULT=data\resultado_auditoria_twap_transfer_v094a.json"
set "REPORT=auditoria_forward_twap_transfer_v094a_7_dias.txt"

echo ============================================================
echo FASE 4.2 v0.9.4a1 - FORWARD TWAP + WATCHDOG 7 DIAS
echo ============================================================
echo.
echo Hipotesis: twap_transfer_strike_hgb
echo Modelo NO cambiado; solo se corrige la recuperacion RTDS.
echo Edge base del collector: 0.10
echo Decision: 60 segundos antes del cierre
echo TWAP actual y apertura deben tener maximo 5 s de antiguedad.
echo Watchdog: si no llega TWAP valido por 12 s, reconecta RTDS.
echo NO usa wallet, Kelly ni dinero real.
echo.

if not exist ".venv\Scripts\python.exe" goto :missing_python
if not exist "%PHASE4_DB%" goto :missing_phase4
if not exist "%MODEL_FILE%" goto :missing_model
if exist "%REPORT%" goto :completed

.\.venv\Scripts\python.exe -m polymarket_bot run-shadow --model-file "%MODEL_FILE%" --output-db "%SHADOW_DB%" --hours 168 --max-db-gb 1 --min-free-gb 20 --keep-awake
set "RUN_CODE=%errorlevel%"
if "%RUN_CODE%"=="10" goto :safety_stop
if not "%RUN_CODE%"=="0" goto :interrupted

echo.
echo Auditando forward...
.\.venv\Scripts\python.exe -m polymarket_bot audit-shadow --shadow-db "%SHADOW_DB%" --phase4-db "%PHASE4_DB%" --report-file "%AUDIT_RESULT%"
set "AUDIT_CODE=%errorlevel%"

echo FASE 4.2 v0.9.4a1 - FORWARD TWAP WATCHDOG 7 DIAS > "%REPORT%"
echo Fin: %date% %time% >> "%REPORT%"
echo. >> "%REPORT%"
echo AUDITORIA >> "%REPORT%"
type "%AUDIT_RESULT%" >> "%REPORT%"
echo. >> "%REPORT%"
echo ESTADO FINAL >> "%REPORT%"
.\.venv\Scripts\python.exe -m polymarket_bot shadow-status --shadow-db "%SHADOW_DB%" >> "%REPORT%" 2>&1

echo.
type "%REPORT%"
echo.
echo Dinero real sigue BLOQUEADO aunque el paper candidate pase.
pause
exit /b %AUDIT_CODE%

:completed
echo Ya existe %REPORT%.
type "%REPORT%"
pause
exit /b 0

:missing_python
echo ERROR: falta .venv\Scripts\python.exe
pause
exit /b 2

:missing_phase4
echo ERROR: falta %PHASE4_DB%
pause
exit /b 3

:missing_model
echo ERROR: falta %MODEL_FILE%
pause
exit /b 4

:safety_stop
echo Detenido por proteccion de almacenamiento.
pause
exit /b 10

:interrupted
echo Forward interrumpido. Los datos guardados siguen validos.
echo Ejecute este mismo BAT para reanudar.
pause
exit /b 11

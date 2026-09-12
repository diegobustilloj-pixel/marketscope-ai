@echo off
setlocal
cd /d "%~dp0"

set "MODEL_FILE=data\modelos_twap_transfer_v093.joblib"
set "SHADOW_DB=data\shadow_tecnica_twap_transfer_v093.db"

echo ============================================================
echo FASE 4.2 v0.9.3 - SMOKE TEST TWAP TRANSFER 15 MIN

echo ============================================================
echo.
echo - Crea una base NUEVA.
echo - Prueba TWAP actual + TWAP de apertura.
echo - Puntua SOLO twap_transfer_strike_hgb.
echo - NO usa wallet ni dinero real.
echo.

if not exist ".venv\Scripts\python.exe" goto :missing_python
if not exist "%MODEL_FILE%" goto :missing_model
if exist "%SHADOW_DB%" goto :exists

.\.venv\Scripts\python.exe -m polymarket_bot run-shadow --model-file "%MODEL_FILE%" --output-db "%SHADOW_DB%" --hours 0.25 --max-db-gb 1 --min-free-gb 20 --keep-awake
set "RUN_CODE=%errorlevel%"
if not "%RUN_CODE%"=="0" goto :failed

echo.
echo ESTADO FINAL:
.\.venv\Scripts\python.exe -m polymarket_bot shadow-status --shadow-db "%SHADOW_DB%"
echo.
echo Envie esta salida para confirmar antes del forward de 7 dias.
pause
exit /b 0

:exists
echo ERROR: ya existe %SHADOW_DB%
echo No se sobrescribe automaticamente. Renombre o elimine solo si se confirma.
pause
exit /b 4

:missing_python
echo ERROR: falta .venv\Scripts\python.exe
pause
exit /b 2

:missing_model
echo ERROR: falta %MODEL_FILE%
pause
exit /b 3

:failed
echo La prueba termino con codigo %RUN_CODE%.
.\.venv\Scripts\python.exe -m polymarket_bot shadow-status --shadow-db "%SHADOW_DB%"
pause
exit /b %RUN_CODE%

@echo off
setlocal
cd /d "%~dp0"

set "MODEL_FILE=data\modelos_shadow_fase41.joblib"
set "SHADOW_DB=data\shadow_tecnica_fase42_v091.db"

echo ============================================================
echo FASE 4.2 v0.9.1 - PRUEBA TECNICA TWAP DE 1 HORA
echo ============================================================
echo.
echo - Base nueva: %SHADOW_DB%
echo - Captura Chainlink spot + TWAP oficial 30s.
echo - Modelo Strike heredado BLOQUEADO.
echo - Sin wallet, Kelly ni ordenes reales.
echo - Esta prueba NO valida rentabilidad.
echo.

if not exist ".venv\Scripts\python.exe" goto :missing_python
if not exist "%MODEL_FILE%" goto :missing_model

".venv\Scripts\python.exe" -m polymarket_bot run-shadow --model-file "%MODEL_FILE%" --output-db "%SHADOW_DB%" --hours 1 --max-db-gb 1 --min-free-gb 20 --keep-awake
set "RUN_CODE=%errorlevel%"

echo.
echo ============================================================
echo ESTADO DE LA PRUEBA
 echo ============================================================
".venv\Scripts\python.exe" -m polymarket_bot shadow-status --shadow-db "%SHADOW_DB%"
echo.
echo Envie la salida y el archivo %SHADOW_DB% para revision.
echo NO ejecute iniciar_shadow_forward_7_dias.bat.
pause
exit /b %RUN_CODE%

:missing_python
echo ERROR: no se encontro .venv\Scripts\python.exe
pause
exit /b 2

:missing_model
echo ERROR: no se encontro %MODEL_FILE%
pause
exit /b 3

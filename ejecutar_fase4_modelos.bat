@echo off
setlocal
cd /d "%~dp0"

set "GOLD_DB=data\gold_fase3_v2.db"
set "PHASE4_DB=data\fase4_modelos.db"
set "PHASE4_PARTIAL=%PHASE4_DB%.partial"
set "MODEL_FILE=data\modelo_fase4_seleccionado.joblib"
set "MODEL_PARTIAL=%MODEL_FILE%.partial"
set "RESULT=data\resultado_fase4.json"
set "REPORT=auditoria_fase4_modelos.txt"

echo ============================================================
echo FASE 4 - MODELOS PROBABILISTICOS Y BACKTEST
echo ============================================================
echo.
echo Este proceso:
echo - Lee Gold v2 en modo SOLO LECTURA.
echo - Compara mercado, Markov, momentum, logistica y boosting.
echo - Calibra usando solo una parte cronologica de train.
echo - Selecciona modelo y umbral usando solo validation.
echo - Mantiene test bloqueado si ningun candidato es convincente.
echo - Incluye ask, comision taker cripto y slippage conservador.
echo - NO usa Kelly, wallet, API privada, IA ni dinero real.
echo.
echo Importante:
echo Que el proceso termine bien NO significa que exista rentabilidad.
echo La auditoria separara el estado tecnico del estadistico.
echo.
echo Tiempo estimado: entre 2 y 10 minutos.
echo No cierre esta ventana.
echo.

if not exist "%GOLD_DB%" goto :missing_gold
if exist "%REPORT%" goto :existing
if exist "%RESULT%" goto :existing
if exist "%PHASE4_DB%" goto :existing
if exist "%PHASE4_PARTIAL%" goto :partial
if exist "%MODEL_FILE%" goto :existing
if exist "%MODEL_PARTIAL%" goto :partial

.\.venv\Scripts\python.exe -m polymarket_bot build-models --gold-db "%GOLD_DB%" --output-db "%PHASE4_DB%" --model-file "%MODEL_FILE%" --report-file "%RESULT%"
if errorlevel 9 goto :gate_failed
if errorlevel 1 goto :error

echo FASE 4 - MODELOS Y BACKTEST > "%REPORT%"
echo Inicio de informe: %date% %time% >> "%REPORT%"
echo. >> "%REPORT%"
echo RESULTADO FASE 4 >> "%REPORT%"
type "%RESULT%" >> "%REPORT%"
echo. >> "%REPORT%"
echo ESTADO FASE 4 >> "%REPORT%"
.\.venv\Scripts\python.exe -m polymarket_bot phase4-status --phase4-db "%PHASE4_DB%" >> "%REPORT%" 2>&1
if errorlevel 1 goto :error
echo. >> "%REPORT%"
echo Fin: %date% %time% >> "%REPORT%"

echo.
type "%REPORT%"
echo.
echo ============================================================
echo FASE 4 COMPLETADA TECNICAMENTE.
echo Esto NO confirma que la estrategia sea rentable.
echo Envie auditoria_fase4_modelos.txt para revision profesional.
echo No ejecute paper trading ni elimine las bases anteriores.
echo ============================================================
pause
exit /b 0

:missing_gold
echo.
echo NO SE ENCONTRO:
echo %GOLD_DB%
echo Ejecute primero la correccion Gold v2 aprobada.
pause
exit /b 2

:existing
echo.
echo YA EXISTE UNA SALIDA O INFORME DE FASE 4.
echo No se sobrescribio nada.
echo Envie auditoria_fase4_modelos.txt si existe.
pause
exit /b 6

:partial
echo.
echo EXISTE UNA SALIDA PARCIAL DE FASE 4.
echo No se elimino ni sobrescribio.
echo Envie una captura antes de hacer cambios.
pause
exit /b 6

:gate_failed
echo.
echo EL PROCESO DE FASE 4 NO SUPERO LA AUDITORIA TECNICA.
echo Gold v2 sigue intacta.
echo Envie resultado_fase4.json y una captura.
pause
exit /b 9

:error
echo.
echo FASE 4 SE DETUVO POR UN ERROR TECNICO.
echo Gold v2 sigue intacta.
echo Envie una captura. No elimine archivos .partial.
pause
exit /b 1

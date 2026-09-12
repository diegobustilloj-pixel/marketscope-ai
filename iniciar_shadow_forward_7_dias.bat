@echo off
setlocal
cd /d "%~dp0"

set "GOLD_DB=data\gold_fase3_v2.db"
set "PHASE4_DB=data\fase4_modelos.db"
set "MODEL_FILE=data\modelos_shadow_fase41.joblib"
set "MODEL_PARTIAL=%MODEL_FILE%.partial"
set "PREP_RESULT=data\resultado_preparacion_shadow.json"
set "SHADOW_DB=data\shadow_forward_fase41.db"
set "AUDIT_RESULT=data\resultado_auditoria_shadow.json"
set "REPORT=auditoria_forward_7_dias.txt"

echo ============================================================
echo FASE 4.1 - SHADOW FORWARD TESTING DE 7 DIAS
echo ============================================================
echo.
echo Esta prueba:
echo - Congela tres hipotesis de 60 segundos.
echo - Mantiene bloqueado el test historico.
echo - Usa Binance, Chainlink, Gamma y CLOB publicos.
echo - Guarda solo una observacion agregada por mercado.
echo - Se puede reanudar ejecutando nuevamente este mismo archivo.
echo - Para si la base llega a 1 GB.
echo - Para si quedan menos de 20 GB libres.
echo - NO usa wallet, claves, Kelly, IA ni dinero real.
echo.
echo Duracion total: 168 horas desde el primer inicio.
echo Mantenga el equipo conectado al cargador y a internet.
echo Puede apagar la pantalla, pero no apague ni suspenda el equipo.
echo.

if not exist "%GOLD_DB%" goto :missing_gold
if not exist "%PHASE4_DB%" goto :missing_phase4
if exist "%REPORT%" goto :completed
if exist "%MODEL_PARTIAL%" goto :partial

if exist "%MODEL_FILE%" goto :run

echo Preparando y congelando los tres modelos...
.\.venv\Scripts\python.exe -m polymarket_bot prepare-shadow --gold-db "%GOLD_DB%" --phase4-db "%PHASE4_DB%" --output-model "%MODEL_FILE%" --report-file "%PREP_RESULT%"
if errorlevel 1 goto :prepare_error

:run
echo.
echo Iniciando o reanudando la prueba shadow...
echo No cierre esta ventana.
echo.
.\.venv\Scripts\python.exe -m polymarket_bot run-shadow --model-file "%MODEL_FILE%" --output-db "%SHADOW_DB%" --hours 168 --max-db-gb 1 --min-free-gb 20 --keep-awake
set "RUN_CODE=%errorlevel%"
if "%RUN_CODE%"=="10" goto :safety_stop
if not "%RUN_CODE%"=="0" goto :interrupted

echo.
echo Auditando los siete dias...
.\.venv\Scripts\python.exe -m polymarket_bot audit-shadow --shadow-db "%SHADOW_DB%" --phase4-db "%PHASE4_DB%" --report-file "%AUDIT_RESULT%"
set "AUDIT_CODE=%errorlevel%"

echo FASE 4.1 - SHADOW FORWARD 7 DIAS > "%REPORT%"
echo Fin de ejecucion: %date% %time% >> "%REPORT%"
echo. >> "%REPORT%"
if exist "%PREP_RESULT%" (
  echo PREPARACION DE MODELOS >> "%REPORT%"
  type "%PREP_RESULT%" >> "%REPORT%"
  echo. >> "%REPORT%"
)
echo AUDITORIA FORWARD >> "%REPORT%"
type "%AUDIT_RESULT%" >> "%REPORT%"
echo. >> "%REPORT%"
echo ESTADO FINAL >> "%REPORT%"
.\.venv\Scripts\python.exe -m polymarket_bot shadow-status --shadow-db "%SHADOW_DB%" >> "%REPORT%" 2>&1

echo.
type "%REPORT%"
echo.
echo ============================================================
echo PRUEBA SHADOW FINALIZADA.
echo Envie auditoria_forward_7_dias.txt para revision.
echo No abra el test historico ni ejecute dinero real.
echo ============================================================
if "%AUDIT_CODE%"=="12" (
  echo La captura termino, pero no supero algun control tecnico.
  echo El informe conserva el detalle para diagnostico.
)
pause
exit /b %AUDIT_CODE%

:missing_gold
echo.
echo NO SE ENCONTRO:
echo %GOLD_DB%
pause
exit /b 2

:missing_phase4
echo.
echo NO SE ENCONTRO:
echo %PHASE4_DB%
pause
exit /b 3

:completed
echo.
echo LA PRUEBA YA TIENE UN INFORME FINAL.
echo Envie auditoria_forward_7_dias.txt.
pause
exit /b 0

:partial
echo.
echo EXISTE UN MODELO PARCIAL.
echo No se elimino ni sobrescribio. Envie una captura.
pause
exit /b 6

:prepare_error
echo.
echo NO SE PUDIERON CONGELAR LOS MODELOS.
echo Gold, Fase 4 y el test historico siguen intactos.
echo Envie una captura.
pause
exit /b 7

:safety_stop
echo.
echo LA PRUEBA SE DETUVO POR UNA PROTECCION DE ALMACENAMIENTO.
echo No elimine archivos. Envie una captura.
pause
exit /b 10

:interrupted
echo.
echo LA PRUEBA FUE INTERRUMPIDA O WINDOWS SE CERRO.
echo Los datos guardados siguen validos.
echo Para continuar, ejecute otra vez iniciar_shadow_forward_7_dias.bat.
pause
exit /b 11

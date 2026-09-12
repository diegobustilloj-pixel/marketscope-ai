@echo off
setlocal
cd /d "%~dp0"

set "V3_DB=C:\ProyectoBotV3\polymarket_quant_bot\data\polymarket_soak_72h_v3.db"
set "V4_DB=%CD%\data\polymarket_fuentes_2h_v4.db"
set "V3_OUT=data\silver_completo_v3_865.db"
set "V4_OUT=data\silver_completo_v4_25.db"
set "V3_PARTIAL=%V3_OUT%.partial"
set "V4_PARTIAL=%V4_OUT%.partial"
set "V3_RESULT=data\resultado_silver_v3.json"
set "V4_RESULT=data\resultado_silver_v4.json"
set "LABEL_CACHE=data\gamma_resoluciones_fase2.json"
set "REPORT=auditoria_fase2_completa.txt"

echo ============================================================
echo FASE 2 - PROCESAMIENTO COMPLETO SILVER
echo ============================================================
echo.
echo Este proceso:
echo - Procesa los 865 mercados V3 y los 25 mercados V4.
echo - Abre las bases raw exclusivamente en modo SOLO LECTURA.
echo - Produce unas 267.000 filas, una por segundo de mercado.
echo - Conserva un cache reanudable de etiquetas Gamma verificadas.
echo - No usa wallet, claves, IA ni dinero.
echo.
echo Tiempo estimado: entre 60 y 90 minutos.
echo La ventana mostrara avances. No la cierre.
echo.

if not exist "%V3_DB%" (
  set "V3_DB=C:\ProyectoBotV3\data\polymarket_soak_72h_v3.db"
)
if not exist "%V3_DB%" goto :missing_v3
if not exist "%V4_DB%" goto :missing_v4
if exist "%REPORT%" goto :existing_report
if exist "%V3_PARTIAL%" goto :partial
if exist "%V4_PARTIAL%" goto :partial

if exist "%V3_OUT%" (
  if not exist "%V3_RESULT%" goto :inconsistent
  echo La salida V3 completa ya esta aprobada. Se reutilizara.
  goto :v4
)

echo ============================================================
echo PASO 1 DE 2 - PROCESANDO V3, 865 MERCADOS
echo ============================================================
.\.venv\Scripts\python.exe -m polymarket_bot build-silver --source-db "%V3_DB%" --output-db "%V3_OUT%" --label-cache "%LABEL_CACHE%" --report-file "%V3_RESULT%" --keep-awake
if errorlevel 7 goto :gate_failed
if errorlevel 1 goto :error

:v4
if exist "%V4_OUT%" (
  if not exist "%V4_RESULT%" goto :inconsistent
  echo La salida V4 completa ya esta aprobada. Se reutilizara.
  goto :report
)

echo.
echo ============================================================
echo PASO 2 DE 2 - PROCESANDO V4, 25 MERCADOS
echo ============================================================
.\.venv\Scripts\python.exe -m polymarket_bot build-silver --source-db "%V4_DB%" --output-db "%V4_OUT%" --label-cache "%LABEL_CACHE%" --report-file "%V4_RESULT%" --keep-awake
if errorlevel 7 goto :gate_failed
if errorlevel 1 goto :error

:report
echo FASE 2 COMPLETA > "%REPORT%"
echo Inicio de informe: %date% %time% >> "%REPORT%"
echo. >> "%REPORT%"
echo RESULTADO V3 >> "%REPORT%"
type "%V3_RESULT%" >> "%REPORT%"
echo. >> "%REPORT%"
echo ESTADO V3 >> "%REPORT%"
.\.venv\Scripts\python.exe -m polymarket_bot silver-status --silver-db "%V3_OUT%" >> "%REPORT%" 2>&1
if errorlevel 1 goto :error

echo. >> "%REPORT%"
echo RESULTADO V4 >> "%REPORT%"
type "%V4_RESULT%" >> "%REPORT%"
echo. >> "%REPORT%"
echo ESTADO V4 >> "%REPORT%"
.\.venv\Scripts\python.exe -m polymarket_bot silver-status --silver-db "%V4_OUT%" >> "%REPORT%" 2>&1
if errorlevel 1 goto :error
echo. >> "%REPORT%"
echo Fin: %date% %time% >> "%REPORT%"

echo.
type "%REPORT%"
echo.
echo ============================================================
echo FASE 2 COMPLETA Y APROBADA.
echo Envie auditoria_fase2_completa.txt para revision.
echo No elimine todavia las bases raw V3 o V4.
echo ============================================================
pause
exit /b 0

:missing_v3
echo.
echo NO SE ENCONTRO LA BASE V3 DE 72 HORAS.
echo Revise que C:\ProyectoBotV3 siga en su ubicacion original.
pause
exit /b 2

:missing_v4
echo.
echo NO SE ENCONTRO:
echo %V4_DB%
echo Extraiga esta version encima de la carpeta V4 aprobada.
pause
exit /b 3

:existing_report
echo.
echo YA EXISTE auditoria_fase2_completa.txt.
echo No se sobrescribio nada. Envie ese archivo para revision.
pause
exit /b 6

:partial
echo.
echo EXISTE UNA SALIDA .partial DE UN INTENTO ANTERIOR.
echo No se elimino ni sobrescribio. Envie una captura para revisarla.
pause
exit /b 6

:inconsistent
echo.
echo EXISTE UNA BASE SILVER SIN SU INFORME JSON.
echo No se sobrescribio nada. Envie una captura antes de continuar.
pause
exit /b 6

:gate_failed
echo.
echo EL PROCESAMIENTO TERMINO, PERO NO SUPERO TODOS LOS CONTROLES.
echo Las bases raw siguen intactas.
echo Envie los archivos de resultado disponibles y una captura.
pause
exit /b 7

:error
echo.
echo EL PROCESAMIENTO SE DETUVO POR UN ERROR TECNICO.
echo Las bases raw siguen intactas y las etiquetas verificadas siguen en cache.
echo Envie una captura. No elimine archivos .partial.
pause
exit /b 1

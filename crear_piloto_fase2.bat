@echo off
setlocal
cd /d "%~dp0"

set "V3_DB=C:\ProyectoBotV3\polymarket_quant_bot\data\polymarket_soak_72h_v3.db"
set "V4_DB=%CD%\data\polymarket_fuentes_2h_v4.db"
set "V3_OUT=data\silver_pilot_v3_10.db"
set "V4_OUT=data\silver_pilot_v4_10.db"
set "REPORT=auditoria_fase2_piloto.txt"

echo ============================================================
echo FASE 2 - PILOTO SILVER - 10 MERCADOS V3 + 10 MERCADOS V4
echo ============================================================
echo.
echo Este proceso:
echo - Abre las bases raw en modo de SOLO LECTURA.
echo - Consulta resultados publicos oficiales de Polymarket.
echo - Crea bases Silver nuevas y pequenas.
echo - No usa wallet, claves, IA ni dinero.
echo.
echo Puede tardar varios minutos. No cierre esta ventana.
echo.

if not exist "%V3_DB%" (
  set "V3_DB=C:\ProyectoBotV3\data\polymarket_soak_72h_v3.db"
)
if not exist "%V3_DB%" goto :missing_v3
if not exist "%V4_DB%" goto :missing_v4
if exist "%V3_OUT%" goto :existing
if exist "%V4_OUT%" goto :existing
if exist "%REPORT%" goto :existing

echo PILOTO FASE 2 > "%REPORT%"
echo Inicio: %date% %time% >> "%REPORT%"
echo. >> "%REPORT%"

echo Procesando 10 mercados de la captura V3...
.\.venv\Scripts\python.exe -m polymarket_bot build-silver --source-db "%V3_DB%" --output-db "%V3_OUT%" --max-markets 10 >> "%REPORT%" 2>&1
if errorlevel 7 goto :gate_failed
if errorlevel 1 goto :error

echo.
echo Procesando 10 mercados de la captura V4...
.\.venv\Scripts\python.exe -m polymarket_bot build-silver --source-db "%V4_DB%" --output-db "%V4_OUT%" --max-markets 10 >> "%REPORT%" 2>&1
if errorlevel 7 goto :gate_failed
if errorlevel 1 goto :error

echo. >> "%REPORT%"
echo ESTADO SILVER V3 >> "%REPORT%"
.\.venv\Scripts\python.exe -m polymarket_bot silver-status --silver-db "%V3_OUT%" >> "%REPORT%" 2>&1
if errorlevel 1 goto :error

echo. >> "%REPORT%"
echo ESTADO SILVER V4 >> "%REPORT%"
.\.venv\Scripts\python.exe -m polymarket_bot silver-status --silver-db "%V4_OUT%" >> "%REPORT%" 2>&1
if errorlevel 1 goto :error

echo. >> "%REPORT%"
echo Fin: %date% %time% >> "%REPORT%"

echo.
type "%REPORT%"
echo.
echo ============================================================
echo PILOTO FASE 2 COMPLETADO Y APROBADO.
echo Envie el archivo auditoria_fase2_piloto.txt para revision.
echo No ejecute todavia el procesamiento de los 865 mercados.
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
echo Extraiga esta version encima de la carpeta V4 que uso en la prueba.
pause
exit /b 3

:existing
echo.
echo YA EXISTE UNA SALIDA O INFORME DEL PILOTO.
echo No se sobrescribio nada. Envie una captura antes de eliminar archivos.
pause
exit /b 6

:gate_failed
echo.
echo EL PILOTO TERMINO, PERO NO SUPERO TODOS LOS CONTROLES.
echo Las bases raw siguen intactas.
echo Envie auditoria_fase2_piloto.txt para revisar el motivo.
pause
exit /b 7

:error
echo.
echo EL PILOTO SE DETUVO POR UN ERROR TECNICO.
echo Las bases raw siguen intactas.
echo Envie auditoria_fase2_piloto.txt y una captura.
pause
exit /b 1

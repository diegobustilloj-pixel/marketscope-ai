@echo off
setlocal
cd /d "%~dp0"

set "V3_SILVER=data\silver_completo_v3_865.db"
set "V4_SILVER=data\silver_completo_v4_25.db"
set "GOLD_OUT=data\gold_fase3_v2.db"
set "GOLD_PARTIAL=%GOLD_OUT%.partial"
set "RESULT=data\resultado_gold_fase3_v2.json"
set "REPORT=auditoria_fase3_gold_v2.txt"

echo ============================================================
echo FASE 3 - CORRECCION GOLD V2
echo ============================================================
echo.
echo Motivo:
echo - Gold v1 descarto 348 mercados sin priceToBeat oficial.
echo - V2 conservara los 885 mercados aptos.
echo - NO inventara los priceToBeat faltantes.
echo - La ausencia quedara marcada explicitamente.
echo.
echo Este proceso:
echo - Lee Silver en modo SOLO LECTURA.
echo - Crea una base nueva; no modifica ni borra Gold v1.
echo - Repite la auditoria temporal completa.
echo - No entrena modelos, no usa IA, wallet ni dinero.
echo.
echo Tiempo estimado: entre 2 y 10 minutos.
echo No cierre esta ventana.
echo.

if not exist "%V3_SILVER%" goto :missing_v3
if not exist "%V4_SILVER%" goto :missing_v4
if exist "%REPORT%" goto :existing
if exist "%GOLD_OUT%" goto :existing
if exist "%GOLD_PARTIAL%" goto :partial
if exist "%RESULT%" goto :existing

.\.venv\Scripts\python.exe -m polymarket_bot build-gold --v3-silver-db "%V3_SILVER%" --v4-silver-db "%V4_SILVER%" --output-db "%GOLD_OUT%" --report-file "%RESULT%"
if errorlevel 8 goto :gate_failed
if errorlevel 1 goto :error

echo FASE 3 GOLD V2 > "%REPORT%"
echo Inicio de informe: %date% %time% >> "%REPORT%"
echo. >> "%REPORT%"
echo RESULTADO GOLD V2 >> "%REPORT%"
type "%RESULT%" >> "%REPORT%"
echo. >> "%REPORT%"
echo ESTADO GOLD V2 >> "%REPORT%"
.\.venv\Scripts\python.exe -m polymarket_bot gold-status --gold-db "%GOLD_OUT%" >> "%REPORT%" 2>&1
if errorlevel 1 goto :error
echo. >> "%REPORT%"
echo Fin: %date% %time% >> "%REPORT%"

echo.
type "%REPORT%"
echo.
echo ============================================================
echo GOLD V2 COMPLETADA Y APROBADA POR LOS CONTROLES AUTOMATICOS.
echo Envie auditoria_fase3_gold_v2.txt para revision.
echo No elimine Gold v1 ni las bases Silver o raw.
echo ============================================================
pause
exit /b 0

:missing_v3
echo.
echo NO SE ENCONTRO:
echo %V3_SILVER%
pause
exit /b 2

:missing_v4
echo.
echo NO SE ENCONTRO:
echo %V4_SILVER%
pause
exit /b 3

:existing
echo.
echo YA EXISTE UNA SALIDA O INFORME GOLD V2.
echo No se sobrescribio nada. Envie auditoria_fase3_gold_v2.txt si existe.
pause
exit /b 6

:partial
echo.
echo EXISTE UNA SALIDA GOLD V2 .partial.
echo No se elimino ni sobrescribio. Envie una captura.
pause
exit /b 6

:gate_failed
echo.
echo GOLD V2 NO SUPERO TODOS LOS CONTROLES.
echo Las bases anteriores siguen intactas.
echo Envie resultado_gold_fase3_v2.json y una captura.
pause
exit /b 8

:error
echo.
echo GOLD V2 SE DETUVO POR UN ERROR TECNICO.
echo Las bases anteriores siguen intactas.
echo Envie una captura. No elimine archivos .partial.
pause
exit /b 1

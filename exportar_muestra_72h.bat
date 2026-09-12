@echo off
setlocal
cd /d "%~dp0"
set "SOURCE_DB=C:\ProyectoBotV3\polymarket_quant_bot\data\polymarket_soak_72h_v3.db"
set "OUTPUT_ZIP=export_72h_muestra.zip"

echo ============================================================
echo EXPORTAR MUESTRA PEQUENA DE LAS 72 HORAS
echo ============================================================
echo.
echo La base original se abrira en modo de SOLO LECTURA.
echo No se borrara, movera ni modificara ningun dato.
echo.

if not exist "%SOURCE_DB%" (
  set "SOURCE_DB=C:\ProyectoBotV3\data\polymarket_soak_72h_v3.db"
)
if not exist "%SOURCE_DB%" goto :missing
if exist "%OUTPUT_ZIP%" goto :existing

echo Base encontrada:
echo %SOURCE_DB%
echo.
.\.venv\Scripts\python.exe -m polymarket_bot export-v3 --source-db "%SOURCE_DB%" --output "%OUTPUT_ZIP%"
if errorlevel 1 goto :error

echo.
echo ============================================================
echo EXPORTACION COMPLETADA.
echo Archivo creado: %CD%\%OUTPUT_ZIP%
echo Envie ese ZIP para analizarlo. NO envie la base de 16.5 GB.
echo Conserve la base original por ahora.
echo ============================================================
pause
exit /b 0

:missing
echo.
echo NO SE ENCONTRO LA BASE V3 EN LAS DOS RUTAS ESPERADAS:
echo C:\ProyectoBotV3\polymarket_quant_bot\data\polymarket_soak_72h_v3.db
echo C:\ProyectoBotV3\data\polymarket_soak_72h_v3.db
echo Tome una captura de este mensaje.
pause
exit /b 2

:existing
echo.
echo YA EXISTE %OUTPUT_ZIP%
echo No se sobrescribio. Mueva el ZIP existente antes de repetir.
pause
exit /b 3

:error
echo.
echo LA EXPORTACION NO PUDO COMPLETARSE.
echo La base original permanece intacta. Tome una captura.
pause
exit /b 1

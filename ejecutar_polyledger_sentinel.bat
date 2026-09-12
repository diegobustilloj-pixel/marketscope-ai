@echo off
setlocal
cd /d "%~dp0"

set "DEFAULT_WALLET=0x5a218c7ad04135830a45c41aaed7294df7809318"
set /p "TARGET_WALLET=Wallet publica 0x (Enter para Balthazar): "
if not defined TARGET_WALLET set "TARGET_WALLET=%DEFAULT_WALLET%"

if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
  set "PYTHON_EXE=python"
)

echo.
echo Auditando %TARGET_WALLET% en modo de solo lectura...
"%PYTHON_EXE%" polyledger_sentinel_v001.py --wallet "%TARGET_WALLET%"
if errorlevel 1 (
  echo.
  echo La auditoria no termino. Revise el error mostrado arriba.
  pause
  exit /b 1
)

echo.
echo Informe: data\polyledger\ultimo_reporte.md
start "" "data\polyledger\ultimo_reporte.md"
pause


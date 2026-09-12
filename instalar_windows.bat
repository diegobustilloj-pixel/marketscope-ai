@echo off
setlocal
cd /d "%~dp0"
echo %CD% | findstr /I "OneDrive" >nul
if not errorlevel 1 (
  echo ADVERTENCIA: Esta carpeta esta dentro de OneDrive.
  echo Para las pruebas use una carpeta como C:\ProyectoBotV4
  echo.
)
py -m venv .venv
if errorlevel 1 goto :error
.\.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.\.venv\Scripts\python.exe -m pip install -e .
if errorlevel 1 goto :error
.\.venv\Scripts\python.exe -c "import polymarket_bot; print('Bot instalado:', polymarket_bot.__version__)"
if errorlevel 1 goto :error
echo.
echo Instalacion Fase 4.1 v0.8.0 completada correctamente.
echo La comprobacion no escribio en ninguna base de datos.
echo.
echo Siguiente paso autorizado:
echo iniciar_shadow_forward_7_dias.bat
echo.
echo NO repita las pruebas de 72 ni de 2 horas.
pause
exit /b 0

:error
echo.
echo La instalacion no pudo completarse. Revise que Python 3.11 o superior este instalado.
pause
exit /b 1

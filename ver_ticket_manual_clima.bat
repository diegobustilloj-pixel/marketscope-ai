@echo off
setlocal
cd /d "%~dp0"
call actualizar_senal_clima.bat
exit /b %errorlevel%

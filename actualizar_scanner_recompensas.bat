@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" reward_mm_shadow.py
) else (
  py reward_mm_shadow.py
)
echo.
echo Resultado guardado en data\reward_mm_shadow_v001
pause

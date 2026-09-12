@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" reward_mm_shadow.py --fast-only --scan-markets 100 --duration-hours 24 --poll-seconds 60
) else (
  py reward_mm_shadow.py --fast-only --scan-markets 100 --duration-hours 24 --poll-seconds 60
)
echo.
echo Shadow finalizado. Revise data\reward_mm_shadow_v001
pause

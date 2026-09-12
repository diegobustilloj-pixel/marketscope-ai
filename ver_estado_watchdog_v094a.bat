@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -c "import sqlite3; p=r'data\shadow_forward_twap_transfer_v094a.db'; c=sqlite3.connect('file:'+p+'?mode=ro',uri=True); r=c.execute('SELECT recorded_at,counters_json,connections_json FROM shadow_health ORDER BY recorded_at DESC LIMIT 1').fetchone(); print('LATEST HEALTH:',r if r else 'SIN HEALTH'); print('FEATURES:',c.execute('SELECT COUNT(*) FROM shadow_features').fetchone()[0]); print('TWAP MODEL:',c.execute('SELECT COUNT(*) FROM shadow_signals WHERE model_name=?',('twap_transfer_strike_hgb',)).fetchone()[0]); c.close()"
echo.
pause

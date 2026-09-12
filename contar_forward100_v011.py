import json
import sqlite3
from pathlib import Path

DB = Path(r"data\shadow_forward_twap_transfer_v094a.db")
CUTOFF_MS = 1786394100000

def fresh(raw):
    d = json.loads(raw)
    return (
        d.get("twap_30s_fresh") in (1, True)
        and d.get("twap_open_fresh") in (1, True)
    )

con = sqlite3.connect(DB)

f60 = con.execute(
    """
    SELECT m.condition_id, m.market_start_ms, f.feature_json
    FROM shadow_features f
    JOIN shadow_markets m ON m.condition_id=f.condition_id
    WHERE f.horizon_seconds=60
      AND f.feature_json IS NOT NULL
      AND m.label_verified=1
      AND m.market_start_ms > ?
    ORDER BY m.market_start_ms, m.condition_id
    """,
    (CUTOFF_MS,),
).fetchall()

d120 = dict(
    con.execute(
        """
        SELECT condition_id, feature_json
        FROM shadow_diagnostics
        WHERE horizon_seconds=120
          AND status='SAVED'
          AND feature_json IS NOT NULL
        """
    )
)

con.close()

f60_fresh = [r for r in f60 if fresh(r[2])]
consumidos = f60_fresh[:50]
remaining = f60_fresh[50:]
eligible = [
    r for r in remaining
    if r[0] in d120 and fresh(d120[r[0]])
]

print("POST-CUTOFF 60S FRESH TOTAL:", len(f60_fresh))
print("YA CONSUMIDOS DESARROLLO:", len(consumidos))
print("FORWARD100 ELEGIBLES ACUMULADOS:", len(eligible))
print("FALTAN PARA 100:", max(0, 100 - len(eligible)))
print("LISTO PARA EVALUAR:", len(eligible) >= 100)
print("NOTA: este script no lee label, outcome ni PnL; solo disponibilidad/frescura.")

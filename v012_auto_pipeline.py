from __future__ import annotations

import json
import time
from pathlib import Path

import calibrar_v012_execution_ev as cal
import evaluar_v012_development as dev
import evaluar_v012_forward100 as fwd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

AUDIT50 = DATA / "auditoria_tecnica_v012_50.json"
THRESHOLDS = DATA / "prereg_v012_thresholds.json"
DEV_RESULT = DATA / "resultado_v012_development100.json"
FWD_RESULT = DATA / "resultado_v012_forward100.json"

POLL_SECONDS = 30


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    cal.verify()

    print("=" * 92)
    print("V0.12 AUTO PIPELINE - PAPER ONLY")
    print("=" * 92)
    print("50 elegibles  -> auditoria tecnica SIN labels/PnL")
    print("100 elegibles -> congela thresholds SIN labels/PnL")
    print("100 elegibles -> evalua DEVELOPMENT100 una sola vez")
    print("200 elegibles -> evalua FORWARD100 solo si development paso")
    print("NO retuning | NO partial peeking | DINERO REAL BLOQUEADO")
    print("Ctrl+C solo detiene este monitor; no afecta los collectors.")
    print("=" * 92)

    last_n = -1

    try:
        while True:
            e = cal.eligible()
            n = len(e)

            if n != last_n:
                print(f"[STATUS] elegibles={n} | faltan50={max(0,50-n)} | faltan100={max(0,100-n)} | faltan200={max(0,200-n)}")
                last_n = n

            if n >= 50 and not AUDIT50.exists():
                print("\n>>> Ejecutando AUDIT50...")
                cal.audit50()

            if n >= 100 and not THRESHOLDS.exists():
                print("\n>>> Congelando thresholds con DEVELOPMENT100, SIN labels/PnL...")
                cal.freeze_thresholds()

            if n >= 100 and THRESHOLDS.exists() and not DEV_RESULT.exists():
                print("\n>>> Evaluando DEVELOPMENT100...")
                dev.evaluate()

            if DEV_RESULT.exists():
                d = load(DEV_RESULT)
                status = d.get("status")
                if status == "FAIL_DEVELOPMENT":
                    print("\nDEVELOPMENT100 FALLO. Pipeline terminado.")
                    print("FORWARD100 NO se toca. DINERO REAL BLOQUEADO.")
                    return

                if status == "PASS_DEVELOPMENT":
                    if n >= 200 and not FWD_RESULT.exists():
                        print("\n>>> Evaluando FORWARD100 completo...")
                        fwd.evaluate_forward()

                    if FWD_RESULT.exists():
                        r = load(FWD_RESULT)
                        print("\n" + "=" * 92)
                        print("PIPELINE V0.12 COMPLETADO")
                        print("DEVELOPMENT:", status)
                        print("FORWARD:", r.get("status"))
                        print("CANDIDATO:", r.get("candidate"))
                        print("TRADES FORWARD:", r.get("trades"))
                        print("PNL 5 SHARES:", r.get("net_pnl_5shares"))
                        print("ROI:", r.get("roi_on_cost"))
                        print("DINERO REAL: BLOQUEADO")
                        print("=" * 92)
                        return

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        print("\nMonitor v0.12 detenido. Los collectors siguen independientes.")


if __name__ == "__main__":
    main()

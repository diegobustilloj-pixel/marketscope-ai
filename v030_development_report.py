from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.v030_development import run_development_screen


ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "data" / "paper_v029_high_frequency_holdout.db"
DEFAULT_SOURCE_RESULT = ROOT / "data" / "resultado_v029_high_frequency_holdout.json"
DEFAULT_OUTPUT = ROOT / "data" / "diagnostico_v030_resolution_mechanics_final.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Filtro de desarrollo V0.30; solo lectura y sin ordenes."
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--source-result", default=str(DEFAULT_SOURCE_RESULT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    result = run_development_screen(
        database=args.database,
        source_result_path=args.source_result,
        output_path=args.output,
    )
    evaluation = result["evaluation"]
    print("=" * 90)
    print("V0.30 - FILTRO MECANICO DE RESOLUCION - DESARROLLO CERRADO")
    print("=" * 90)
    print(f"Decision: {evaluation['decision']}")
    print(
        "Mercados utilizables: "
        f"{evaluation['usable_markets']} | senales: {evaluation['candidate_signal_count']}"
    )
    metrics = evaluation["candidate_metrics"]
    print(
        "PnL a 5: "
        f"{metrics['net_pnl_at_5_shares']} | PF: {metrics['profit_factor']} | "
        f"LCB95: {metrics['one_sided_95_lcb']}"
    )
    print("Ordenes: 0 | wallet: no requerida | dinero real: BLOQUEADO")
    print(json.dumps(evaluation["gate_results"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

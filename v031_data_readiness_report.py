from __future__ import annotations

from pathlib import Path

from polymarket_bot.v031_data_readiness import write_readiness_report


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "diagnostico_v031_path_execution_readiness.json"


def main() -> int:
    result = write_readiness_report(project_root=ROOT, output_path=OUTPUT)
    print("=" * 90)
    print("V0.31 - DISPONIBILIDAD DE DATOS PARA TRAYECTORIA Y SALIDA")
    print("=" * 90)
    print(f"Estado: {result['status']}")
    print(f"Decision: {result['decision']}")
    print(f"Bloqueos: {len(result['blockers'])}")
    print("Ordenes: 0 | outcomes: 0 | wallet: no requerida | dinero real: BLOQUEADO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

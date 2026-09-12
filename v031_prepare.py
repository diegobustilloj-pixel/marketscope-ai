from __future__ import annotations

from pathlib import Path

from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v031_prereg import build_frozen_prereg


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "prereg_v031_path_execution_capture.json"


def main() -> int:
    payload = build_frozen_prereg(output_path=OUTPUT, project_root=ROOT)
    print("V0.31 CAPTURE DESIGN: FROZEN")
    print(f"Archivo: {OUTPUT}")
    print(f"SHA256: {sha256_file(OUTPUT)}")
    print(f"Estado: {payload['implementation']['launch_status']}")
    print("Ordenes: 0 | outcomes: 0 | dinero real: BLOQUEADO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

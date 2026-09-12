from __future__ import annotations

import argparse
import json
from pathlib import Path

from polymarket_bot.evidence_pack import (
    ROOT,
    EvidencePackError,
    write_or_verify_evidence_pack,
)


DEFAULT_MANIFEST_DIR = ROOT / "data" / "evidence_pack_manifests"
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "evidence_packs"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Genera o verifica Evidence Packs reproducibles sin leer el forward "
            "activo ni habilitar dinero real."
        )
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=DEFAULT_MANIFEST_DIR,
        help="Directorio de manifiestos JSON.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directorio de salida inmutable.",
    )
    args = parser.parse_args()

    manifests = sorted(args.manifest_dir.resolve().glob("*.json"))
    if not manifests:
        parser.error(f"No hay manifiestos JSON en {args.manifest_dir}")

    results = []
    try:
        for manifest in manifests:
            results.append(
                write_or_verify_evidence_pack(
                    manifest,
                    args.output_dir,
                    root=ROOT,
                )
            )
    except EvidencePackError as exc:
        parser.exit(2, f"ERROR EVIDENCE PACK: {exc}\n")

    print(
        json.dumps(
            {
                "status": "OK",
                "packs": results,
                "active_forward_read": False,
                "active_forward_modified": False,
                "real_money": "BLOQUEADO",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

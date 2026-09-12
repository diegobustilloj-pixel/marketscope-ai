from __future__ import annotations

import json

from polymarket_bot.v018_runner import ROOT
from polymarket_bot.v028_design import build_v028_design_evidence
from polymarket_bot.v028_prereg import build_frozen_prereg


POSTMORTEM = ROOT / "data" / "postmortem_v027_up_low_vol_final.json"
RESULT = ROOT / "data" / "resultado_v027_regime_tournament.json"
DESIGN = ROOT / "data" / "diagnostico_v028_twap_lt5_candidate.json"
PREREG = ROOT / "data" / "prereg_v028_twap_lt5_replication.json"


def main() -> int:
    design = build_v028_design_evidence(
        postmortem_path=POSTMORTEM,
        result_path=RESULT,
        output_path=DESIGN,
    )
    prereg = build_frozen_prereg(
        design_path=DESIGN,
        output_path=PREREG,
        project_root=ROOT,
    )
    print(
        json.dumps(
            {
                "design": str(DESIGN),
                "design_schema": design["schema"],
                "preregistration": str(PREREG),
                "preregistration_schema": prereg["schema"],
                "status": prereg["status"],
                "launch_status": prereg["implementation"]["launch_status"],
                "runner_built": prereg["implementation"]["runner_built"],
                "orders_enabled": prereg["safety"]["orders_enabled"],
                "wallet_required": prereg["safety"]["wallet_required"],
                "real_money": prereg["safety"]["real_money"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

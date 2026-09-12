from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = DATA / "analisis_resultados_hasta_20260814.json"
SOURCES = {
    "data/resultado_v011_event_driven_development.json": "37996f51f0cd16e031b7fce2e72eb1902614f039da08cfccc5e31d18b76b8e75",
    "data/resultado_v011_forward100.json": "b01ff07c19b97c063a58ae003f2e5d7b8036657c6a42786582f7ea03d94f5cef",
    "data/resultado_v012_development100.json": "a1698fb71326c6c5500c270cc2f07eb9576547700b7a396866c5cffca9fde488",
    "data/resultado_v013_forward10.json": "b0dc66cf0e044fe17fe9c2d58bce68845bae24d664219785269b61d1d40b15cd",
    "data/resultado_v014_development12.json": "c6e88e026294605083b03e032b6a36d68e6e00c0e773e0bc0b0de0b9b0963a69",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_frozen(relative: str, expected: str) -> dict[str, Any]:
    path = (ROOT / relative).resolve()
    if sha256(path) != expected:
        raise RuntimeError(f"Resultado sellado cambio: {relative}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Resultado incompatible: {relative}")
    return payload


def frequency_projection(trades: int, eligible: int, target: int) -> dict[str, Any]:
    observed = trades / eligible if eligible else 0.0
    maximum_markets_24h = 24 * 12
    optimistic_trades = observed * maximum_markets_24h
    required = target / maximum_markets_24h
    return {
        "trades": trades,
        "eligible_markets": eligible,
        "target_trades": target,
        "observed_rate_per_eligible": observed,
        "maximum_5m_markets_in_24h": maximum_markets_24h,
        "optimistic_expected_trades_if_every_market_were_eligible": optimistic_trades,
        "minimum_rate_needed_even_if_every_market_were_eligible": required,
        "rate_increase_factor_needed": required / observed if observed else None,
        "unchanged_rule_feasible_in_24h_at_observed_rate": optimistic_trades >= target,
        "projection_is_optimistic": True,
    }


def build_analysis() -> dict[str, Any]:
    results = {
        relative: load_frozen(relative, expected)
        for relative, expected in SOURCES.items()
    }
    v011_dev = results["data/resultado_v011_event_driven_development.json"]
    v011_forward = results["data/resultado_v011_forward100.json"]
    v012 = results["data/resultado_v012_development100.json"]
    v013 = results["data/resultado_v013_forward10.json"]
    v014 = results["data/resultado_v014_development12.json"]
    strict_wide = v012["candidate_results"]["strict_wide"]
    v013_projection = frequency_projection(
        int(v013["qualifying_trades"]),
        int(v013["eligible_markets_examined"]),
        10,
    )
    v014_low = frequency_projection(
        int(v014["low_trades"]),
        int(v014["eligible_markets_examined"]),
        6,
    )
    v014_moderate = frequency_projection(
        int(v014["moderate_trades"]),
        int(v014["eligible_markets_examined"]),
        6,
    )
    return {
        "schema": "analisis_resultados_24h_policy_1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_hashes": SOURCES,
        "evidence": {
            "v011_development": {
                "status": v011_dev["verdict"],
                "selected_rule": v011_dev["selected_rule"]["family"]
                + "/"
                + v011_dev["selected_rule"]["severity"],
                "trades": v011_dev["selected_rule"]["development_metrics"]["trades"],
                "roi_on_cost": v011_dev["selected_rule"]["development_metrics"]["roi_on_cost"],
            },
            "v011_forward100": {
                "status": v011_forward["verdicto"],
                "trades": v011_forward["events_traded"],
                "net_pnl": v011_forward["net_pnl"],
                "roi_on_cost": v011_forward["roi_on_cost"],
                "failures": v011_forward["failures"],
            },
            "v012_development100": {
                "status": v012["status"],
                "selected_candidate": v012["selected_candidate"],
                "best_descriptive_candidate": "strict_wide",
                "best_descriptive_trades": strict_wide["trades"],
                "best_descriptive_net_pnl_5shares": strict_wide["net_pnl_5shares"],
                "best_descriptive_roi_on_cost": strict_wide["roi_on_cost"],
                "best_descriptive_failures": strict_wide["failures"],
            },
            "v013": {
                "status": v013["status"],
                "labels_read": v013["labels_read"],
                "pnl_calculated": v013["pnl_calculated"],
                "frequency": v013_projection,
            },
            "v014": {
                "status": v014["status"],
                "labels_read": v014["labels_read"],
                "pnl_calculated": v014["pnl_calculated"],
                "selected_for_v015": v014["selected_for_v015"],
                "low_frequency": v014_low,
                "moderate_frequency": v014_moderate,
            },
        },
        "decision": {
            "current_twap_shock_market_lag_candidate": "CLOSED_AS_CURRENTLY_DEFINED",
            "reason": (
                "Fallo forward en v0.11, fallo de desarrollo en v0.12 y "
                "frecuencia insuficiente sin PnL evaluable en v0.13/v0.14"
            ),
            "threshold_rescue_allowed": False,
            "v015_applicable": False,
            "next_primary_evidence": "WAIT_OFFICIAL_7D_TWAP_TRANSFER_AUDIT",
            "next_secondary_evidence": "ANALYZE_PREREGISTERED_FINAL_24H_AFTER_7D_COMPLETES",
            "future_experiment_maximum_hours": 24.0,
            "money_real_candidate": False,
        },
        "forward_7d_outcomes_read_by_this_analysis": 0,
        "orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


def write_report(payload: dict[str, Any]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_name(OUTPUT.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, OUTPUT)


def main() -> None:
    payload = build_analysis()
    write_report(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

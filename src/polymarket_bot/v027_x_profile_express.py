from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.phase4 import _taker_cost_per_share
from polymarket_bot.v018_runner import _parse_utc, sha256_file
from polymarket_bot.v024_tournament import FEE_RATE, SLIPPAGE_PER_SHARE, market_record
from polymarket_bot.v025_audit import sequence_metrics


EXPRESS_SCHEMA = "diagnostic_v027_x_profiles_express_1"
RESULT_SCHEMA = "result_v026b_adaptive_checkpoints_1"
RECONCILIATION_SCHEMA = "reconciliation_v026b_terminal_audit_1"
OFFICIAL_TWAP_REFERENCE = (
    "https://polymarket.com/event/btc-updown-5m-1787080800?outcomeIndex=0"
)
OFFICIAL_TWAP_DOCUMENTATION = (
    "https://docs.polymarket.com/market-data/chainlink-twap"
)


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _json_meta(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        str(key): json.loads(str(value))
        for key, value in connection.execute("SELECT key,value FROM shadow_meta")
    }


def _outcome_record(
    *,
    feature: Mapping[str, Any],
    side: str,
    label: str,
    market_start_ms: int,
    condition_id: str,
) -> dict[str, Any] | None:
    if side not in {"Up", "Down"}:
        raise ValueError("Lado invalido")
    ask = feature.get("up_best_ask" if side == "Up" else "down_best_ask")
    if ask is None:
        return None
    entry_cost, fill_price, fee = _taker_cost_per_share(
        float(ask),
        fee_rate=FEE_RATE,
        slippage_per_share=SLIPPAGE_PER_SHARE,
    )
    return {
        "condition_id": condition_id,
        "market_start_ms": market_start_ms,
        "label": label,
        # sequence_metrics usa este campo como el lado comprado, aunque su nombre
        # historico sea favorite_side.
        "favorite_side": side,
        "entry_cost": entry_cost,
        "fill_price": fill_price,
        "fee": fee,
    }


def _hour_block(market_start_ms: int, start_ms: int) -> str:
    elapsed = max(0.0, (market_start_ms - start_ms) / 3_600_000)
    start = min(20, int(elapsed // 4) * 4)
    return f"h{start:02d}_to_h{start + 4:02d}"


def _summaries(
    rows: Sequence[Mapping[str, Any]], *, start_ms: int
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: int(row["market_start_ms"]))
    midpoint_ms = start_ms + 12 * 3_600_000
    blocks = {
        f"h{hour:02d}_to_h{hour + 4:02d}": sequence_metrics(
            [
                row
                for row in ordered
                if _hour_block(int(row["market_start_ms"]), start_ms)
                == f"h{hour:02d}_to_h{hour + 4:02d}"
            ]
        )
        for hour in range(0, 24, 4)
    }
    return {
        "full_window": sequence_metrics(ordered),
        "first_12h": sequence_metrics(
            [row for row in ordered if int(row["market_start_ms"]) < midpoint_ms]
        ),
        "second_12h": sequence_metrics(
            [row for row in ordered if int(row["market_start_ms"]) >= midpoint_ms]
        ),
        "four_hour_blocks": blocks,
    }


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_x_profile_express_diagnostic(
    *,
    result_path: str | Path,
    reconciliation_path: str | Path,
    output_path: str | Path | None = None,
    official_market_twap_window_seconds: int = 60,
) -> dict[str, Any]:
    """Screen social hypotheses against the already-closed V0.26b evidence.

    This is deliberately exploratory. It does not turn social claims into a
    preregistered candidate and it never creates orders or new backtest hours.
    """

    result_file = Path(result_path).resolve()
    reconciliation_file = Path(reconciliation_path).resolve()
    output_file = Path(output_path).resolve() if output_path is not None else None
    result = json.loads(result_file.read_text(encoding="utf-8"))
    reconciliation = json.loads(reconciliation_file.read_text(encoding="utf-8"))
    if result.get("schema") != RESULT_SCHEMA:
        raise RuntimeError("Resultado V0.26b incompatible")
    if reconciliation.get("schema") != RECONCILIATION_SCHEMA:
        raise RuntimeError("Reconciliacion V0.26b incompatible")
    if reconciliation.get("corrected_verdict") != "FAIL_REPLICATION":
        raise RuntimeError("La reconciliacion V0.26b no esta cerrada")
    if official_market_twap_window_seconds not in {30, 60}:
        raise ValueError("La ventana TWAP oficial debe ser 30 o 60 segundos")

    database = Path(str(result["database"])).resolve()
    hashes_before = {
        "result": sha256_file(result_file),
        "reconciliation": sha256_file(reconciliation_file),
        "database": sha256_file(database),
    }
    if hashes_before["database"] != str(result.get("database_sha256")):
        raise RuntimeError("La base V0.26b no coincide con su resultado")
    if output_file is not None and output_file.is_file():
        existing = json.loads(output_file.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == EXPRESS_SCHEMA
            and existing.get("source_hashes") == hashes_before
            and existing.get("contract_check", {}).get(
                "official_market_twap_window_seconds"
            )
            == official_market_twap_window_seconds
        ):
            return existing
        raise RuntimeError("Existe un diagnostico exprés para otra evidencia")

    start = _parse_utc(str(result["window"]["experiment_started_at"]))
    cutoff = _parse_utc(str(result["window"]["target_end_at"]))
    start_ms = int(start.timestamp() * 1000)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    favorite_rows: list[dict[str, Any]] = []
    underdog_rows: list[dict[str, Any]] = []
    feature_rows = 0
    missing_favorite_ask = 0
    missing_underdog_ask = 0

    connection = _open_read_only(database)
    try:
        meta = _json_meta(connection)
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        twap_windows = {
            str(int(window_s)): int(count)
            for window_s, count in connection.execute(
                "SELECT window_s,COUNT(*) FROM shadow_twap_ticks GROUP BY window_s"
            )
        }
        for raw in connection.execute(
            """
            SELECT m.condition_id,m.market_start_ms,m.label,f.feature_json
            FROM shadow_markets AS m
            JOIN shadow_features AS f USING(condition_id)
            WHERE m.label_verified=1
              AND m.market_start_ms>=? AND m.market_start_ms<=?
            ORDER BY m.market_start_ms
            """,
            (start_ms, cutoff_ms),
        ):
            feature_rows += 1
            feature = json.loads(str(raw["feature_json"]))
            favorite = market_record(
                feature_json=feature,
                model_probability_up=None,
                label=str(raw["label"]),
                market_start_ms=int(raw["market_start_ms"]),
                condition_id=str(raw["condition_id"]),
            )
            if favorite is None:
                missing_favorite_ask += 1
                continue
            favorite_rows.append(favorite)
            underdog_side = "Down" if favorite["favorite_side"] == "Up" else "Up"
            underdog = _outcome_record(
                feature=feature,
                side=underdog_side,
                label=str(raw["label"]),
                market_start_ms=int(raw["market_start_ms"]),
                condition_id=str(raw["condition_id"]),
            )
            if underdog is None:
                missing_underdog_ask += 1
            else:
                underdog_rows.append(underdog)
    finally:
        connection.close()

    qwinsi_fill_band = [
        row for row in favorite_rows if 0.60 <= float(row["fill_price"]) <= 0.88
    ]
    qwinsi_cost_band = [
        row for row in favorite_rows if 0.60 <= float(row["entry_cost"]) <= 0.88
    ]
    moon_underdog = [
        row for row in underdog_rows if float(row["fill_price"]) < 0.60
    ]
    laoying_longshot = [
        row for row in underdog_rows if 0.01 <= float(row["fill_price"]) <= 0.08
    ]
    punisher_near_certain = [
        row for row in favorite_rows if float(row["fill_price"]) >= 0.95
    ]
    punisher_queue_099 = [
        row for row in favorite_rows if float(row["fill_price"]) >= 0.985
    ]

    captured_window = int(meta.get("twap_window_seconds", 0))
    mismatch = captured_window != official_market_twap_window_seconds
    safety = {
        "database_query_only": query_only == 1,
        "sqlite_quick_check": quick_check,
        "source_evidence_modified": False,
        "new_backtest_hours": 0,
        "orders_created": False,
        "paper_orders": 0,
        "wallet_required": bool(meta.get("wallet_required", False)),
        "real_money": str(meta.get("v026b_real_money", "UNKNOWN")),
        "orders_disabled_passed": meta.get("orders_enabled") is False,
        "money_real_disabled_passed": meta.get("money_real_enabled") is False,
    }
    payload: dict[str, Any] = {
        "schema": EXPRESS_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "closed_read_only_posthoc_social_hypothesis_screen",
        "source_files": {
            "result": str(result_file),
            "reconciliation": str(reconciliation_file),
            "database": str(database),
        },
        "source_hashes": hashes_before,
        "window": {
            "start": start.isoformat(),
            "cutoff": cutoff.isoformat(),
            "hours": 24,
            "feature_rows": feature_rows,
            "favorite_rows": len(favorite_rows),
            "underdog_rows": len(underdog_rows),
            "missing_favorite_ask": missing_favorite_ask,
            "missing_underdog_ask": missing_underdog_ask,
        },
        "contract_check": {
            "official_market_twap_window_seconds": official_market_twap_window_seconds,
            "captured_twap_window_seconds": captured_window,
            "captured_twap_topics": meta.get("rtds_topics"),
            "captured_twap_tick_windows": twap_windows,
            "window_mismatch": mismatch,
            "official_reference": OFFICIAL_TWAP_REFERENCE,
            "official_stream_documentation": OFFICIAL_TWAP_DOCUMENTATION,
            "directly_affected": [
                "twap_transfer_strike_hgb signal",
                "twap_distance_to_open_bps",
                "TWAP alignment and distance segment findings",
            ],
            "not_directly_affected": [
                "favorite-side price and verified-label settlement records",
                "taker fee and slippage calculation",
                "volatility_regime_ratio derived from Chainlink spot history",
            ],
        },
        "hypotheses": {
            "qwinsi_favorite_entry_060_088": {
                "source": "https://x.com/qwinsi0x/status/2090439622123823595",
                "status": "PARTIAL_PROXY_ONLY",
                "what_was_tested": (
                    "Favorite outcome at a simulated taker fill price from 0.60 to "
                    "0.88, held to settlement."
                ),
                "what_was_not_tested": (
                    "The claimed fixed-profit exit, actual queue/fill probability, "
                    "position sizing, and unmatched notional risk."
                ),
                "fill_price_band": _summaries(qwinsi_fill_band, start_ms=start_ms),
                "total_entry_cost_band_sensitivity": _summaries(
                    qwinsi_cost_band, start_ms=start_ms
                ),
                "by_side": {
                    side: sequence_metrics(
                        [row for row in qwinsi_fill_band if row["favorite_side"] == side]
                    )
                    for side in ("Up", "Down")
                },
                "decision": "DO_NOT_PROMOTE_FROM_POSTHOC_PROXY",
            },
            "moondev_underdog_below_060": {
                "source": "https://x.com/MoonDevOnYT/status/2067200591516889556",
                "status": "BROAD_SETTLEMENT_PROXY_ONLY",
                "what_was_tested": (
                    "The non-favorite outcome below a 0.60 simulated fill, held to settlement."
                ),
                "what_was_not_tested": "The post does not specify a complete exit rule.",
                "metrics": _summaries(moon_underdog, start_ms=start_ms),
                "decision": "DO_NOT_PROMOTE_VAGUE_RULE",
            },
            "laoying_longshot_001_008_exit_020_060": {
                "source": "https://x.com/laoyingkhq/status/2090756347956727989",
                "status": "UNTESTABLE_EXIT_WITH_CURRENT_DATABASE",
                "frequency_at_decision_snapshot": len(laoying_longshot),
                "settlement_only_upper_risk_proxy": _summaries(
                    laoying_longshot, start_ms=start_ms
                ),
                "reason": (
                    "V0.26b stores one decision snapshot, not the later price path needed "
                    "to determine whether a 0.20-0.60 exit was reachable and fillable."
                ),
                "decision": "REJECT_AS_V027_RULE",
            },
            "punisher_reader_sweeper_near_099": {
                "source": "https://x.com/0x_Punisher/status/2081362888397070432",
                "status": "UNTESTABLE_EXECUTION_STRATEGY_WITH_CURRENT_DATABASE",
                "near_095_snapshot_count": len(punisher_near_certain),
                "near_0985_snapshot_count": len(punisher_queue_099),
                "settlement_only_proxy_near_095": _summaries(
                    punisher_near_certain, start_ms=start_ms
                ),
                "reason": (
                    "A 60-second decision snapshot cannot reconstruct FIFO queue position, "
                    "fill probability, ghost fills, settlement latency, or capital recycling."
                ),
                "decision": "RESCUE_EXECUTION_CONTROLS_NOT_THE_EDGE_CLAIM",
            },
            "moondev_liquidation_bot": {
                "source": "https://x.com/MoonDevOnYT/status/2090499106380615984",
                "status": "NO_REPRODUCIBLE_RULE_IN_POST",
                "decision": "NO_TEST_NO_PROMOTION",
            },
            "laoying_claude_lag_arbitrage": {
                "source": "https://x.com/laoyingkhq/status/2090313012741894161",
                "status": "CLAIMS_CONTRADICTED_AND_LINKED_REPOSITORY_NOT_AN_AUTOTRADER",
                "decision": "REJECT",
            },
        },
        "application_decision": {
            "apply_now": [
                "resolution-contract guard before any fresh forward",
                "explicit separation of signal, execution, and settlement evidence",
                "chain/PnL reconciliation and kill-switch concepts for future execution rails",
            ],
            "do_not_apply": [
                "social PnL screenshots as evidence of causality",
                "fixed-profit or longshot rules without price-path and fill data",
                "automatic trading or real-money tests",
                "claims that overfitting is irrelevant",
            ],
            "v027_status": (
                "BLOCKED_PENDING_VERSIONED_TWAP_60S_COMPATIBILITY"
                if mismatch
                else "CONTRACT_COMPATIBLE_NOT_LAUNCHED"
            ),
            "frozen_v027_modified": False,
            "next_safe_step": (
                "Create a compatibility-preserving collector version that subscribes to "
                "the market's declared TWAP window, then run only a short technical capture "
                "before deciding whether V0.27 needs a new preregistration."
            ),
        },
        "safety": safety,
    }
    hashes_after = {
        "result": sha256_file(result_file),
        "reconciliation": sha256_file(reconciliation_file),
        "database": sha256_file(database),
    }
    if hashes_after != hashes_before:
        raise RuntimeError("La evidencia V0.26b cambio durante el diagnostico")
    if not (
        safety["database_query_only"]
        and safety["sqlite_quick_check"] == "ok"
        and safety["orders_disabled_passed"]
        and safety["money_real_disabled_passed"]
        and safety["wallet_required"] is False
        and safety["real_money"] == "BLOQUEADO"
    ):
        raise RuntimeError("Fallo una puerta de seguridad del diagnostico exprés")
    if output_file is not None:
        _atomic_write(output_file, payload)
    return payload


__all__ = [
    "EXPRESS_SCHEMA",
    "OFFICIAL_TWAP_DOCUMENTATION",
    "OFFICIAL_TWAP_REFERENCE",
    "build_x_profile_express_diagnostic",
]

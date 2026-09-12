from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_bot.phase41 import audit_shadow_forward
from polymarket_bot.v018_runner import sha256_file
from polymarket_bot.v023_runner import VARIANT, load_and_verify_prereg


RESULT_SCHEMA = "result_v023_filtered_forward_4h_1"
MINIMUM_VALIDATION_TRADES = 10
CONFIDENCE_Z = 1.6448536269514715


def _value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def signal_passes_filter(row: Mapping[str, Any]) -> bool:
    return (
        str(_value(row, "model_name")) == "twap_transfer_strike_hgb"
        and int(_value(row, "would_trade", 0) or 0) == 1
        and _value(row, "entry_cost") is not None
        and float(row["entry_cost"]) > 0.50
        and float(_value(row, "expected_edge", 0.0) or 0.0) >= 0.10
        and float(_value(row, "expected_edge", 0.0) or 0.0) < 0.15
    )


def filtered_trade_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pnls: list[float] = []
    total_cost = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    cumulative = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    wins = 0
    for row in rows:
        if not signal_passes_filter(row):
            continue
        side = str(row["side"])
        label = str(row["label"])
        cost = float(row["entry_cost"])
        won = side == label
        pnl = 1.0 - cost if won else -cost
        pnls.append(pnl)
        total_cost += cost
        wins += int(won)
        gross_profit += max(0.0, pnl)
        gross_loss += max(0.0, -pnl)
        cumulative += pnl
        peak = max(peak, cumulative)
        maximum_drawdown = max(maximum_drawdown, peak - cumulative)
    if not pnls:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "net_pnl_per_share_sequence": 0.0,
            "net_pnl_at_5_shares": 0.0,
            "roi_on_cost": None,
            "profit_factor": None,
            "mean_pnl_per_share": None,
            "pnl_standard_deviation": None,
            "one_sided_95_lcb_mean_pnl": None,
            "maximum_drawdown_per_share": 0.0,
        }
    mean = statistics.fmean(pnls)
    standard_deviation = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    standard_error = standard_deviation / math.sqrt(len(pnls))
    net = sum(pnls)
    return {
        "trades": len(pnls),
        "wins": wins,
        "win_rate": round(wins / len(pnls), 8),
        "net_pnl_per_share_sequence": round(net, 8),
        "net_pnl_at_5_shares": round(net * 5.0, 8),
        "roi_on_cost": round(net / total_cost, 8) if total_cost else None,
        "profit_factor": (
            round(gross_profit / gross_loss, 8) if gross_loss else None
        ),
        "mean_pnl_per_share": round(mean, 8),
        "pnl_standard_deviation": round(standard_deviation, 8),
        "one_sided_95_lcb_mean_pnl": round(
            mean - CONFIDENCE_Z * standard_error, 8
        ),
        "maximum_drawdown_per_share": round(maximum_drawdown, 8),
    }


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def audit_v023(
    *,
    database: str | Path,
    prereg_path: str | Path,
    phase4_db: str | Path,
    result_path: str | Path | None = None,
) -> dict[str, Any]:
    database_path = Path(database).resolve()
    prereg_file = Path(prereg_path).resolve()
    phase4_path = Path(phase4_db).resolve()
    output = Path(result_path).resolve() if result_path is not None else None
    prereg = load_and_verify_prereg(prereg_file)
    if not database_path.is_file():
        raise RuntimeError("Base V0.23 no encontrada")
    database_hash = sha256_file(database_path)
    prereg_hash = sha256_file(prereg_file)
    if output is not None and output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if (
            existing.get("schema") == RESULT_SCHEMA
            and existing.get("database_sha256") == database_hash
            and existing.get("preregistration_sha256") == prereg_hash
        ):
            return existing
        raise RuntimeError("Existe un resultado V0.23 para otra evidencia")

    technical = audit_shadow_forward(
        shadow_db=database_path, phase4_db=phase4_path
    )
    connection = _open_read_only(database_path)
    try:
        meta = {
            str(key): json.loads(str(value))
            for key, value in connection.execute("SELECT key,value FROM shadow_meta")
        }
        all_signal_rows = connection.execute(
            """
            SELECT s.model_name,s.would_trade,s.entry_cost,s.expected_edge,
             s.side,m.label,m.label_verified,m.market_start_ms
            FROM shadow_signals AS s
            JOIN shadow_markets AS m USING(condition_id)
            WHERE s.model_name='twap_transfer_strike_hgb'
              AND s.would_trade=1
            ORDER BY m.market_start_ms
            """
        ).fetchall()
        filtered_total = sum(signal_passes_filter(row) for row in all_signal_rows)
        resolved_rows = [
            row
            for row in all_signal_rows
            if signal_passes_filter(row) and int(row["label_verified"] or 0) == 1
        ]
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()

    performance = filtered_trade_metrics(resolved_rows)
    safety_passed = (
        meta.get("orders_enabled") is False
        and meta.get("money_real_enabled") is False
        and meta.get("v023_real_money") == "BLOQUEADO"
        and technical.get("orders_created") is False
        and technical.get("wallet_required") is False
    )
    technical_passed = bool(technical.get("technical_passed")) and quick_check == "ok"
    lcb = performance["one_sided_95_lcb_mean_pnl"]
    factor = performance["profit_factor"]
    validation_candidate = (
        safety_passed
        and technical_passed
        and performance["trades"] >= MINIMUM_VALIDATION_TRADES
        and performance["net_pnl_per_share_sequence"] > 0
        and lcb is not None
        and lcb > 0
        and (factor is None or factor > 1.0)
    )
    if not safety_passed:
        verdict = "FAIL_SAFETY"
    elif not technical_passed:
        verdict = "FAIL_TECHNICAL_QUALITY"
    elif performance["trades"] == 0:
        verdict = "FAIL_INSUFFICIENT_FREQUENCY"
    elif validation_candidate:
        verdict = "PASS_FILTER_VALIDATION_CANDIDATE"
    elif performance["net_pnl_per_share_sequence"] <= 0:
        verdict = "FAIL_PILOT_PNL"
    else:
        verdict = "PASS_PILOT_ONLY"

    result = {
        "schema": RESULT_SCHEMA,
        "variant": VARIANT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "validation_candidate": validation_candidate,
        "forward_candidate": validation_candidate,
        "money_real_candidate": False,
        "real_money": "BLOQUEADO",
        "wallet_required": False,
        "orders_created": False,
        "paper_orders": 0,
        "database": str(database_path),
        "database_sha256": database_hash,
        "database_read_only_verified": database_hash == sha256_file(database_path),
        "preregistration": str(prereg_file),
        "preregistration_sha256": prereg_hash,
        "filter": prereg["strategy"],
        "window": {
            "target_hours": float(meta.get("target_hours", 0)),
            "started_at": meta.get("experiment_started_at"),
            "target_end_at": meta.get("target_end_at"),
            "completed_at": meta.get("experiment_completed_at"),
            "expected_markets": 48,
        },
        "coverage": {
            "markets": technical.get("markets"),
            "market_coverage": technical.get("market_coverage"),
            "features": technical.get("features"),
            "feature_coverage": technical.get("feature_coverage"),
            "resolved": technical.get("resolved"),
            "resolution_coverage": technical.get("resolution_coverage"),
        },
        "filter_application": {
            "baseline_directional_signals": len(all_signal_rows),
            "filtered_signals": filtered_total,
            "resolved_filtered_signals": len(resolved_rows),
            "excluded_signals": len(all_signal_rows) - filtered_total,
        },
        "performance": performance,
        "gates": {
            "safety_passed": safety_passed,
            "technical_passed": technical_passed,
            "minimum_validation_trades": MINIMUM_VALIDATION_TRADES,
            "minimum_validation_trades_passed": (
                performance["trades"] >= MINIMUM_VALIDATION_TRADES
            ),
            "positive_net_pnl_passed": (
                performance["net_pnl_per_share_sequence"] > 0
            ),
            "positive_lcb_passed": lcb is not None and lcb > 0,
            "profit_factor_above_one_passed": factor is None or factor > 1.0,
        },
        "development_expectation_not_a_promise": {
            "expected_signals_per_4h": 1.619048,
            "expected_pnl_at_5_shares_per_4h": 0.675681,
            "selection_bias_warning": (
                "El forward de siete dias selecciono el filtro; solo esta ventana "
                "futura puede aportar evidencia independiente."
            ),
        },
        "technical_audit": {
            "sqlite_quick_check": technical.get("sqlite_quick_check"),
            "experiment_complete": technical.get("experiment_complete"),
            "technical_passed": technical.get("technical_passed"),
            "phase4_opened_read_only": technical.get("phase4_opened_read_only"),
            "phase4_unchanged": technical.get("phase4_unchanged"),
        },
        "meaning": (
            "PASS_PILOT_ONLY demuestra PnL positivo en esta ventana de cuatro horas, "
            "pero menos de diez operaciones no valida rentabilidad. Ningun veredicto "
            "V0.23 habilita dinero real automaticamente."
        ),
    }
    if output is not None:
        _write_atomic(output, result)
    return result


__all__ = [
    "CONFIDENCE_Z",
    "MINIMUM_VALIDATION_TRADES",
    "RESULT_SCHEMA",
    "audit_v023",
    "filtered_trade_metrics",
    "signal_passes_filter",
]

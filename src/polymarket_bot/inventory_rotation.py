from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .runtime_policy import backtest_runtime_guard, enforce_backtest_runtime_hours


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DEFAULT_PREREG = DATA / "prereg_v0161_inventory_rotation.json"
DEFAULT_RESULT = DATA / "resultado_v0161_inventory_rotation.json"
DEFAULT_TRADES = DATA / "resultado_v0161_inventory_rotation_trades.csv"
PREREG_SCHEMA_V1 = "prereg_v016_inventory_rotation_1"
PREREG_SCHEMA_V2 = "prereg_v016_inventory_rotation_2"
RESULT_SCHEMAS = {
    PREREG_SCHEMA_V1: "resultado_v016_inventory_rotation_1",
    PREREG_SCHEMA_V2: "resultado_v016_inventory_rotation_2",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_utc_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("El timestamp debe incluir zona horaria")
    return int(parsed.timestamp() * 1000)


def taker_cost(
    ask: float,
    *,
    fee_rate: float,
    slippage_per_share: float,
) -> tuple[float, float, float]:
    price = float(ask)
    if not math.isfinite(price):
        raise ValueError("Ask no finita")
    fill = min(0.999, max(0.001, price + float(slippage_per_share)))
    fee = float(fee_rate) * fill * (1.0 - fill)
    return fill, fee, fill + fee


def executable_cost(
    row: Mapping[str, Any],
    side: str,
    execution: Mapping[str, Any],
) -> dict[str, float] | None:
    flags = int(row.get("quality_flags") or 0)
    required_clear_mask = execution.get("required_clear_quality_mask")
    if required_clear_mask is None:
        if flags != 0:
            return None
    elif flags & int(required_clear_mask):
        return None
    side_key = side.lower()
    ask = row.get(f"{side_key}_best_ask")
    depth = row.get(f"{side_key}_ask_depth_1c")
    try:
        ask_value = float(ask)
        depth_value = float(depth)
    except (TypeError, ValueError):
        return None
    shares = float(execution["order_size_shares"])
    if not math.isfinite(depth_value) or depth_value < shares:
        return None
    fill, fee, total = taker_cost(
        ask_value,
        fee_rate=float(execution["fee_rate"]),
        slippage_per_share=float(execution["slippage_per_share"]),
    )
    return {
        "ask": ask_value,
        "fill_price": fill,
        "fee_per_share": fee,
        "total_cost_per_share": total,
        "depth_1c": depth_value,
    }


def simulate_market(
    rows: Iterable[Mapping[str, Any]],
    *,
    condition_id: str,
    market_start_ms: int,
    label: str,
    candidate: Mapping[str, Any],
    execution: Mapping[str, Any],
    strategy: Mapping[str, Any],
) -> dict[str, Any] | None:
    ordered = sorted(rows, key=lambda row: int(row["second_offset"]))
    normalized_label = str(label).upper()
    entry_start, entry_end = [
        int(value) for value in strategy["entry_window_second_offsets_inclusive"]
    ]
    hedge_deadline = int(strategy["hedge_deadline_second_offset_inclusive"])
    entry_cap = float(candidate["entry_cost_cap"])
    pair_cap = float(candidate["pair_cost_cap"])
    shares = float(execution["order_size_shares"])

    entry: dict[str, Any] | None = None
    for row in ordered:
        offset = int(row["second_offset"])
        if offset < entry_start or offset > entry_end:
            continue
        costs = []
        for side in ("UP", "DOWN"):
            cost = executable_cost(row, side, execution)
            if cost is not None and cost["total_cost_per_share"] <= entry_cap:
                costs.append((cost["total_cost_per_share"], side, cost))
        if not costs:
            continue
        _, side, cost = min(costs, key=lambda item: (item[0], item[1] != "UP"))
        entry = {"side": side, "offset": offset, **cost}
        break

    if entry is None:
        return None

    opposite = "DOWN" if entry["side"] == "UP" else "UP"
    hedge: dict[str, Any] | None = None
    for row in ordered:
        offset = int(row["second_offset"])
        if offset <= int(entry["offset"]) or offset > hedge_deadline:
            continue
        cost = executable_cost(row, opposite, execution)
        if cost is None:
            continue
        combined = float(entry["total_cost_per_share"]) + cost[
            "total_cost_per_share"
        ]
        if combined <= pair_cap:
            hedge = {"side": opposite, "offset": offset, **cost}
            break

    entry_cost = shares * float(entry["total_cost_per_share"])
    if hedge is not None:
        combined_cost = float(entry["total_cost_per_share"]) + float(
            hedge["total_cost_per_share"]
        )
        capital = shares * combined_cost
        pnl = shares * (1.0 - combined_cost)
        paired = True
    else:
        if normalized_label not in ("UP", "DOWN"):
            raise RuntimeError(f"Label incompatible para {condition_id}: {label}")
        capital = entry_cost
        pnl = shares * (
            1.0 - float(entry["total_cost_per_share"])
            if normalized_label == entry["side"]
            else -float(entry["total_cost_per_share"])
        )
        paired = False

    return {
        "condition_id": condition_id,
        "market_start_ms": int(market_start_ms),
        "label": normalized_label,
        "candidate": str(candidate["name"]),
        "entry_side": entry["side"],
        "entry_offset": int(entry["offset"]),
        "entry_ask": float(entry["ask"]),
        "entry_fill_price": float(entry["fill_price"]),
        "entry_fee_per_share": float(entry["fee_per_share"]),
        "entry_total_cost_per_share": float(entry["total_cost_per_share"]),
        "paired": paired,
        "hedge_offset": int(hedge["offset"]) if hedge is not None else None,
        "hedge_ask": float(hedge["ask"]) if hedge is not None else None,
        "hedge_fill_price": (
            float(hedge["fill_price"]) if hedge is not None else None
        ),
        "hedge_fee_per_share": (
            float(hedge["fee_per_share"]) if hedge is not None else None
        ),
        "combined_cost_per_share": (
            float(entry["total_cost_per_share"])
            + float(hedge["total_cost_per_share"])
            if hedge is not None
            else None
        ),
        "capital": capital,
        "pnl": pnl,
    }


def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda trade: int(trade["market_start_ms"]))
    count = len(ordered)
    paired = sum(bool(trade["paired"]) for trade in ordered)
    pnl = sum(float(trade["pnl"]) for trade in ordered)
    capital = sum(float(trade["capital"]) for trade in ordered)
    positives = [float(trade["pnl"]) for trade in ordered if trade["pnl"] > 0]

    equity = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    for trade in ordered:
        equity += float(trade["pnl"])
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)

    split = (count + 1) // 2
    first_half = sum(float(trade["pnl"]) for trade in ordered[:split])
    second_half = sum(float(trade["pnl"]) for trade in ordered[split:])
    positive_total = sum(positives)
    largest_share = max(positives) / positive_total if positive_total > 0 else None
    return {
        "entries": count,
        "paired": paired,
        "unpaired": count - paired,
        "paired_rate": paired / count if count else 0.0,
        "positive_trades": len(positives),
        "net_pnl": pnl,
        "capital_deployed": capital,
        "roi_on_cost": pnl / capital if capital > 0 else None,
        "first_half_net_pnl": first_half,
        "second_half_net_pnl": second_half,
        "maximum_drawdown": maximum_drawdown,
        "largest_positive_trade_share": largest_share,
    }


def development_eligible(summary: Mapping[str, Any], gate: Mapping[str, Any]) -> bool:
    largest = summary["largest_positive_trade_share"]
    return bool(
        int(summary["entries"]) >= int(gate["minimum_entries"])
        and float(summary["paired_rate"]) >= float(gate["minimum_paired_rate"])
        and float(summary["net_pnl"]) > 0
        and float(summary["roi_on_cost"] or 0) > 0
        and float(summary["first_half_net_pnl"]) >= 0
        and float(summary["second_half_net_pnl"]) >= 0
        and largest is not None
        and float(largest) <= float(gate["maximum_single_positive_trade_share"])
    )


def validation_passes(summary: Mapping[str, Any], gate: Mapping[str, Any]) -> bool:
    largest = summary["largest_positive_trade_share"]
    return bool(
        int(summary["entries"]) >= int(gate["minimum_entries"])
        and float(summary["paired_rate"]) >= float(gate["minimum_paired_rate"])
        and float(summary["net_pnl"]) > 0
        and float(summary["roi_on_cost"] or 0) > 0
        and largest is not None
        and float(largest) <= float(gate["maximum_single_positive_trade_share"])
        and float(summary["maximum_drawdown"]) <= float(gate["maximum_drawdown"])
    )


def _open_readonly(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    return connection


def evaluate_period(
    connection: sqlite3.Connection,
    *,
    start_ms: int,
    end_ms: int,
    candidate: Mapping[str, Any],
    execution: Mapping[str, Any],
    strategy: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    markets = connection.execute(
        """
        SELECT condition_id, market_start_ms, label
        FROM markets
        WHERE market_start_ms >= ? AND market_start_ms < ?
        ORDER BY market_start_ms, condition_id
        """,
        (start_ms, end_ms),
    ).fetchall()
    trades: list[dict[str, Any]] = []
    for market in markets:
        rows = connection.execute(
            """
            SELECT second_offset, quality_flags,
                   up_best_ask, up_ask_depth_1c,
                   down_best_ask, down_ask_depth_1c
            FROM second_features
            WHERE condition_id = ?
              AND second_offset BETWEEN ? AND ?
            ORDER BY second_offset
            """,
            (
                market["condition_id"],
                int(strategy["entry_window_second_offsets_inclusive"][0]),
                int(strategy["hedge_deadline_second_offset_inclusive"]),
            ),
        ).fetchall()
        trade = simulate_market(
            [dict(row) for row in rows],
            condition_id=str(market["condition_id"]),
            market_start_ms=int(market["market_start_ms"]),
            label=str(market["label"]),
            candidate=candidate,
            execution=execution,
            strategy=strategy,
        )
        if trade is not None:
            trades.append(trade)
    return summarize_trades(trades), trades, len(markets)


def verify_prereg(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    schema = raw.get("schema")
    if schema not in RESULT_SCHEMAS:
        raise RuntimeError("Preregistro v0.16 incompatible")
    if schema == PREREG_SCHEMA_V1:
        payload = raw
        expected_status = "FROZEN_BEFORE_PROFITABILITY_EVALUATION"
    else:
        if raw.get("status") != "FROZEN_BEFORE_CORRECTED_PROFITABILITY_EVALUATION":
            raise RuntimeError("Correccion v0.16.1 no esta congelada")
        parent_meta = raw.get("parent_preregistration", {})
        parent_path = (ROOT / str(parent_meta.get("relative_path", ""))).resolve()
        if not parent_path.is_file() or sha256_file(parent_path) != parent_meta.get(
            "sha256"
        ):
            raise RuntimeError("Preregistro padre v0.16 no coincide")
        parent = json.loads(parent_path.read_text(encoding="utf-8"))
        if parent.get("schema") != PREREG_SCHEMA_V1:
            raise RuntimeError("Schema padre v0.16 incompatible")
        diagnostic_meta = raw.get("diagnostic_result", {})
        diagnostic_path = (
            ROOT / str(diagnostic_meta.get("relative_path", ""))
        ).resolve()
        if not diagnostic_path.is_file() or sha256_file(
            diagnostic_path
        ) != diagnostic_meta.get("sha256"):
            raise RuntimeError("Diagnostico v0.16 no coincide")
        diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
        development = diagnostic.get("development_results", {})
        if (
            diagnostic.get("verdict") != "FAIL_DEVELOPMENT"
            or diagnostic.get("validation_result") is not None
            or any(int(item.get("entries", -1)) != 0 for item in development.values())
        ):
            raise RuntimeError("El diagnostico previo no permite correccion tecnica")
        correction = raw.get("technical_correction", {})
        if (
            int(correction.get("required_clear_quality_mask", -1)) != 22
            or correction.get("thresholds_changed") is not False
            or correction.get("candidate_grid_changed") is not False
            or correction.get("execution_costs_changed") is not False
            or correction.get("gates_changed") is not False
            or int(correction.get("labels_that_influenced_this_correction", -1)) != 0
        ):
            raise RuntimeError("Correccion tecnica v0.16.1 incompatible")
        payload = json.loads(json.dumps(parent))
        payload["schema"] = PREREG_SCHEMA_V2
        payload["status"] = "FROZEN_BEFORE_CORRECTED_PROFITABILITY_EVALUATION"
        payload["created_at"] = raw["created_at"]
        payload["execution_model"]["quality_requirement"] = (
            "(quality_flags & 22) == 0"
        )
        payload["execution_model"]["required_clear_quality_mask"] = 22
        payload["technical_correction"] = correction
        expected_status = "FROZEN_BEFORE_CORRECTED_PROFITABILITY_EVALUATION"
    if payload.get("status") != expected_status:
        raise RuntimeError("Preregistro v0.16 no esta congelado")
    if payload.get("real_money") != "BLOQUEADO":
        raise RuntimeError("Dinero real debe permanecer bloqueado")
    protected = payload.get("protected_forward", {})
    if protected.get("must_not_read") is not True or protected.get("must_not_modify") is not True:
        raise RuntimeError("El forward activo no esta protegido por el preregistro")
    hours = float(payload["backtest_window"]["duration_hours"])
    enforce_backtest_runtime_hours(hours)
    start = parse_utc_ms(payload["backtest_window"]["start_inclusive_utc"])
    end = parse_utc_ms(payload["backtest_window"]["end_exclusive_utc"])
    if end - start != int(hours * 3_600_000):
        raise RuntimeError("La ventana v0.16 no coincide con sus 24 horas declaradas")
    return payload


def run_backtest(prereg_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    prereg = verify_prereg(prereg_path)
    source = ROOT / prereg["historical_source"]["relative_path"]
    if sha256_file(source) != prereg["historical_source"]["sha256"]:
        raise RuntimeError("La base historica no coincide con el hash congelado")

    window = prereg["backtest_window"]
    start_ms = parse_utc_ms(window["start_inclusive_utc"])
    end_ms = parse_utc_ms(window["end_exclusive_utc"])
    development_end_ms = start_ms + 16 * 3_600_000
    if development_end_ms >= end_ms:
        raise RuntimeError("Particion desarrollo/validacion invalida")

    execution = prereg["execution_model"]
    strategy = prereg["strategy"]
    all_details: list[dict[str, Any]] = []
    development: dict[str, Any] = {}
    selected: Mapping[str, Any] | None = None
    with backtest_runtime_guard(float(window["duration_hours"])):
        connection = _open_readonly(source)
        try:
            for candidate in strategy["candidate_grid"]:
                summary, trades, market_count = evaluate_period(
                    connection,
                    start_ms=start_ms,
                    end_ms=development_end_ms,
                    candidate=candidate,
                    execution=execution,
                    strategy=strategy,
                )
                summary["markets"] = market_count
                summary["eligible"] = development_eligible(
                    summary, prereg["development_selection"]
                )
                development[str(candidate["name"])] = summary
                for trade in trades:
                    all_details.append({"phase": "DEVELOPMENT", **trade})

            eligible = [
                candidate
                for candidate in strategy["candidate_grid"]
                if development[str(candidate["name"])]["eligible"]
            ]
            if eligible:
                selected = sorted(
                    eligible,
                    key=lambda item: (
                        -float(development[str(item["name"])]["net_pnl"]),
                        -float(development[str(item["name"])]["roi_on_cost"]),
                        float(item["entry_cost_cap"]),
                    ),
                )[0]

            validation: dict[str, Any] | None = None
            if selected is not None:
                summary, trades, market_count = evaluate_period(
                    connection,
                    start_ms=development_end_ms,
                    end_ms=end_ms,
                    candidate=selected,
                    execution=execution,
                    strategy=strategy,
                )
                summary["markets"] = market_count
                summary["passes"] = validation_passes(
                    summary, prereg["validation_gate"]
                )
                validation = summary
                for trade in trades:
                    all_details.append({"phase": "VALIDATION", **trade})
        finally:
            connection.close()

    if selected is None:
        verdict = "FAIL_DEVELOPMENT"
    elif validation is not None and validation["passes"]:
        verdict = "PASS_HISTORICAL_VALIDATION_PAPER_ONLY"
    else:
        verdict = "FAIL_VALIDATION"

    result = {
        "schema": RESULT_SCHEMAS[str(prereg["schema"])],
        "created_at": utc_now(),
        "verdict": verdict,
        "preregistration": str(prereg_path.resolve()),
        "preregistration_sha256": sha256_file(prereg_path),
        "implementation_sha256": sha256_file(Path(__file__).resolve()),
        "historical_source": str(source.resolve()),
        "historical_source_sha256": prereg["historical_source"]["sha256"],
        "backtest_window": window,
        "technical_correction": prereg.get("technical_correction"),
        "development_results": development,
        "selected_candidate": dict(selected) if selected is not None else None,
        "validation_result": validation,
        "forward_active_read": False,
        "forward_active_modified": False,
        "orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
        "next_action": (
            "Congelar y ejecutar confirmacion paper independiente de hasta 24h"
            if verdict == "PASS_HISTORICAL_VALIDATION_PAPER_ONLY"
            else "No rescatar umbrales con la validacion; cerrar esta version"
        ),
    }
    return result, all_details


def _write_new_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def write_outputs(
    result: Mapping[str, Any],
    details: list[dict[str, Any]],
    *,
    result_path: Path,
    trades_path: Path,
) -> None:
    if result_path.exists() or trades_path.exists():
        raise FileExistsError("Las salidas v0.16 son inmutables y ya existen")
    fields = [
        "phase",
        "candidate",
        "condition_id",
        "market_start_ms",
        "label",
        "entry_side",
        "entry_offset",
        "entry_ask",
        "entry_fill_price",
        "entry_fee_per_share",
        "entry_total_cost_per_share",
        "paired",
        "hedge_offset",
        "hedge_ask",
        "hedge_fill_price",
        "hedge_fee_per_share",
        "combined_cost_per_share",
        "capital",
        "pnl",
    ]
    rows: list[str] = []
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(details)
    rows.append(buffer.getvalue())
    _write_new_text(
        result_path,
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    try:
        _write_new_text(trades_path, "".join(rows))
    except Exception:
        result_path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="V0.16: prueba historica de rotacion temporal UP/DOWN (paper only)"
    )
    parser.add_argument("--prereg", type=Path, default=DEFAULT_PREREG)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULT)
    parser.add_argument("--trades-output", type=Path, default=DEFAULT_TRADES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result, details = run_backtest(args.prereg.resolve())
    write_outputs(
        result,
        details,
        result_path=args.output.resolve(),
        trades_path=args.trades_output.resolve(),
    )
    print("=" * 88)
    print("V0.16 INVENTORY ROTATION - HISTORICO 24H - PAPER ONLY")
    print("=" * 88)
    print(f"Veredicto: {result['verdict']}")
    print(f"Seleccion: {result['selected_candidate']}")
    print(f"Validacion: {result['validation_result']}")
    print("DINERO REAL: BLOQUEADO")
    print(f"Resultado: {args.output.resolve()}")
    return 0


__all__ = [
    "development_eligible",
    "executable_cost",
    "run_backtest",
    "simulate_market",
    "summarize_trades",
    "taker_cost",
    "validation_passes",
]

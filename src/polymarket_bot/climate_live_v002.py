from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
import urllib.parse
from dataclasses import asdict
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .climate_research import (
    GAMMA,
    event_buckets,
    infer_market_date,
    iso,
    parse_resolution_contract,
    stable_json,
    utc_now,
    write_json,
)
from .climate_shadow_monitor import (
    DEFAULT_FORECAST_REFRESH_SECONDS,
    DEFAULT_POLL_SECONDS,
    ClimateShadowError,
    ClimateShadowMonitor,
    MonitorLock,
    create_stop_marker,
    fee_per_share,
    http_json,
    monitor_status,
    write_final_summary,
)


SCHEMA = "climate_live_v002"
DEFAULT_DB = Path("data/climate_live_v002/climate_live_v002.db")
DEFAULT_OUTPUT = Path("data/climate_live_v002")
DEFAULT_RESEARCH_ROOT = Path("data/climate_research_v001")
DISCOVERY_REFRESH_SECONDS = 3600.0
ENTRY_HOUR_LOCAL = 15
ENTRY_WINDOW_MINUTES = 70
PROBABILITY_MIN = 0.30
NET_EDGE_MIN = 0.05
PRICE_MIN = 0.10
PRICE_MAX = 0.75
SPREAD_MAX = 0.10
VISIBLE_NOTIONAL_MIN = 25.0
ADVERSE_PRICE_IMPACT = 0.01
STAKE_USDC = 25.0
TRAIN_MATCH_MIN = 0.98
TRAIN_COMPARABLE_MIN = 30


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def fetch_active_temperature_events() -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    page_size = 100
    for offset in range(0, 2000, page_size):
        params = {
            "limit": page_size,
            "offset": offset,
            "tag_slug": "daily-temperature",
            "closed": "false",
            "active": "true",
            "order": "id",
            "ascending": "false",
        }
        payload = http_json(f"{GAMMA}/events?{urllib.parse.urlencode(params)}")
        if not isinstance(payload, list):
            raise ClimateShadowError("Gamma no devolvió una lista de eventos activos")
        for row in payload:
            if isinstance(row, dict):
                result[str(row.get("id") or row.get("slug"))] = row
        if len(payload) < page_size:
            break
    else:
        raise ClimateShadowError("El descubrimiento activo excedió 2.000 eventos; paginación incompleta")
    return list(result.values())


def operational_contract_v2(
    event: dict[str, Any],
    contract: dict[str, Any],
    station: dict[str, str] | None,
    known_family: set[tuple[str, str, float]],
) -> tuple[bool, str]:
    reasons: list[str] = []
    station_id = str(contract.get("station_id") or "")
    unit = str(contract.get("unit") or "")
    precision = _float(contract.get("precision"))
    description = str(event.get("description") or "")
    source = str(contract.get("source_type") or "")
    if not bool(contract.get("rules_complete")):
        reasons.append("CORE_RULES")
    if not station or station.get("metadata_complete") != "True":
        reasons.append("STATION_METADATA")
    if precision is None or (station_id, unit, precision) not in known_family:
        reasons.append("UNSEEN_STATION_UNIT_PRECISION")
    lower = description.lower()
    if source == "NOAA_NWS":
        phrase_ok = "under the \"temp\" column for all times on this day" in lower
        url_ok = f"site={station_id.lower()}" in lower
        if not (phrase_ok and url_ok):
            reasons.append("NOAA_DAILY_EXTREME_RULE")
    elif source == "WEATHER_UNDERGROUND":
        finalized_daily = "for all times on this day" in lower and "finalized" in lower
        explicit_table = (
            "daily observations" in lower
            and "for all times on this day" in lower
            and bool(contract.get("first_next_day_required"))
        )
        phrase_ok = finalized_daily or explicit_table
        url_ok = station_id.lower() in str(contract.get("resolution_source") or "").lower()
        if not (phrase_ok and url_ok):
            reasons.append("WU_DAILY_EXTREME_RULE")
    elif source == "HONG_KONG_OBSERVATORY":
        if station_id != "HKO" or "hong kong observatory" not in lower:
            reasons.append("HKO_RULE")
    else:
        reasons.append("UNSUPPORTED_SOURCE")
    if station and not station.get("latitude") or station and not station.get("longitude"):
        reasons.append("TIMEZONE_COORDINATES")
    return not reasons, "OK_V2_SOURCE_DAILY_EXTREME_IANA_FROM_COORDINATES" if not reasons else ",".join(reasons)


def _event_fee_schedule(event: dict[str, Any]) -> dict[str, Any] | None:
    schedules = []
    for market in event.get("markets") or []:
        if market.get("feesEnabled") is not True or not isinstance(market.get("feeSchedule"), dict):
            return None
        schedules.append(market["feeSchedule"])
    unique = {stable_json(row) for row in schedules}
    return schedules[0] if schedules and len(unique) == 1 else None


class ClimateLiveMonitorV2(ClimateShadowMonitor):
    def __init__(self, database: Path, research_root: Path, output: Path, *, workers: int = 8) -> None:
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.last_discovery_refresh: datetime | None = None
        super().__init__(database, research_root, workers=workers)
        self.last_discovery_refresh = utc_now()
        self.store.set_meta("schema_version", SCHEMA)
        self.store.set_meta("dynamic_discovery", True)
        self.store.set_meta("entry_policy", "D+1_15:00_LOCAL_70_MINUTES")
        self.store.set_meta("edge_basis", "NET_AFTER_GAMMA_FEE_SCHEDULE_AND_1C_IMPACT")
        self.store.db.commit()

    def _load_markets(self) -> dict[str, dict[str, Any]]:
        stations = {row["station_id"]: row for row in _read_csv(self.derived / "station_catalog.csv")}
        scores = {
            (row["city"], row["market_type"], row["station_id"]): row
            for row in _read_csv(self.derived / "climate_forecastability_scorecard.csv")
        }
        known_family: set[tuple[str, str, float]] = set()
        for row in _read_csv(self.derived / "resolution_contracts_enriched.csv"):
            precision = _float(row.get("precision"))
            if row.get("station_id") and row.get("unit") and precision is not None:
                known_family.add((row["station_id"], row["unit"], precision))
        events = fetch_active_temperature_events()
        captured = {"schema": SCHEMA, "captured_at": iso(utc_now()), "events": events}
        write_json(self.output / "active_events_latest.json", captured)
        today = utc_now().date()
        result: dict[str, dict[str, Any]] = {}
        for event in events:
            try:
                contract = parse_resolution_contract(event)
                station_id = str(contract.get("station_id") or "")
                market_date = infer_market_date(str(event.get("title") or ""), event.get("endDate"))
                if not market_date or not today <= datetime.fromisoformat(market_date).date() <= today + timedelta(days=2):
                    continue
                if station_id not in self.eligible:
                    continue
                strict, reason = operational_contract_v2(event, contract, stations.get(station_id), known_family)
                buckets = [asdict(row) for row in event_buckets(event)]
                schedule = _event_fee_schedule(event)
                market_type = str(contract.get("market_type") or "")
                city = str(contract.get("city") or "")
                score = scores.get((city, market_type, station_id), {})
                result[str(event.get("slug") or "")] = {
                    "slug": str(event.get("slug") or ""),
                    "event_id": str(event.get("id") or ""),
                    "city": city,
                    "market_type": market_type,
                    "market_date": market_date,
                    "station_id": station_id,
                    "unit": str(contract.get("unit") or ""),
                    "precision": float(contract["precision"]),
                    "strict_contract": strict,
                    "contract_reason": reason,
                    "forecastability_score": _float(score.get("forecastability_score")),
                    "sample_grade": score.get("sample_grade") or "UNKNOWN",
                    "buckets": buckets,
                    "fee_schedule": schedule,
                    "source_type": contract.get("source_type"),
                    "resolution_source": contract.get("resolution_source"),
                    "description_sha256": contract.get("description_sha256"),
                }
            except Exception as exc:
                continue
        return result

    def cycle(self, run_id: int, forecast_refresh_seconds: float) -> dict[str, Any]:
        now = utc_now()
        if self.last_discovery_refresh is None or (now - self.last_discovery_refresh).total_seconds() >= DISCOVERY_REFRESH_SECONDS:
            self.markets = self._load_markets()
            self.stations = self._load_stations()
            self.last_discovery_refresh = now
            self.store.set_meta("market_universe", len(self.markets))
            self.store.set_meta("stations", len(self.stations))
            self.store.set_meta("last_discovery_refresh", iso(now))
            self.store.db.commit()
        result = super().cycle(run_id, forecast_refresh_seconds)
        if result.get("status") == "COMPLETE":
            ticket = build_live_ticket(self, int(result["cycle_id"]), now)
            write_live_ticket(ticket, self.output)
            result["manual_ticket"] = {
                "decision": ticket["decision"],
                "current_candidates": len(ticket["current_candidates"]),
                "highest_probability": (ticket.get("highest_quality_d1_probability") or {}).get("top_probability"),
            }
        return result


def _entry_window(market_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    day = datetime.fromisoformat(market_date).date() - timedelta(days=1)
    start = datetime.combine(day, clock_time(ENTRY_HOUR_LOCAL), tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)
    return start, start + timedelta(minutes=ENTRY_WINDOW_MINUTES)


def _profitability_gate(output: Path) -> dict[str, Any]:
    path = output / "backtest" / "backtest_report.json"
    if not path.exists():
        return {"passed": False, "verdict": "MORE DATA REQUIRED", "reason": "No hay backtest oficial acumulado V002"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))["profitability_gate"]
    except (KeyError, json.JSONDecodeError):
        return {"passed": False, "verdict": "MORE DATA REQUIRED", "reason": "Backtest V002 inválido"}


def build_live_ticket(monitor: ClimateLiveMonitorV2, cycle_id: int, observed_at: datetime) -> dict[str, Any]:
    station_gates = {
        row["station_id"]: row for row in _read_csv(monitor.derived / "station_train_quality_gate.csv")
    }
    rows = [dict(row) for row in monitor.store.db.execute(
        """SELECT p.slug,p.observed_at,p.candidate,p.lead_days,p.top_bucket,p.top_probability,
        m.city,m.market_type,m.market_date,m.station_id,m.sample_grade,m.strict_contract,m.contract_reason,
        q.best_ask,q.ask_size,q.spread
        FROM shadow_probabilities p JOIN shadow_markets m ON m.slug=p.slug
        LEFT JOIN shadow_quotes q ON q.cycle_id=p.cycle_id AND q.slug=p.slug AND q.bucket=p.top_bucket AND q.side='YES'
        WHERE p.cycle_id=? ORDER BY p.top_probability DESC""",
        (cycle_id,),
    )]
    evaluated = []
    for row in rows:
        market = monitor.markets.get(row["slug"], {})
        reasons: list[str] = []
        timezone_name = monitor.station_timezones.get(row["station_id"])
        if not timezone_name:
            reasons.append("TIMEZONE_UNAVAILABLE")
            window_start = window_end = None
        else:
            window_start, window_end = _entry_window(row["market_date"], timezone_name)
            if observed_at < window_start:
                reasons.append("TOO_EARLY")
            elif observed_at > window_end:
                reasons.append("WINDOW_EXPIRED")
        if int(row["lead_days"]) != 1:
            reasons.append("NOT_D1")
        if row.get("sample_grade") != "STRONG":
            reasons.append("SAMPLE_NOT_STRONG")
        gate = station_gates.get(row["station_id"])
        if not gate or int(gate["train_comparable"]) < TRAIN_COMPARABLE_MIN or float(gate["train_match_rate"]) < TRAIN_MATCH_MIN:
            reasons.append("STATION_GATE")
        if not bool(row["strict_contract"]):
            reasons.append("CONTRACT")
        probability = float(row["top_probability"])
        if probability < PROBABILITY_MIN:
            reasons.append("PROBABILITY")
        ask = _float(row.get("best_ask"))
        entry_price = min(0.99, ask + ADVERSE_PRICE_IMPACT) if ask is not None else None
        schedule = market.get("fee_schedule")
        fee = fee_per_share(entry_price, schedule) if entry_price is not None else None
        gross_edge = probability - entry_price if entry_price is not None else None
        net_edge = gross_edge - fee if gross_edge is not None and fee is not None else None
        visible_notional = ask * float(row.get("ask_size") or 0.0) if ask is not None else 0.0
        if entry_price is None:
            reasons.append("NO_ASK")
        elif not PRICE_MIN <= entry_price <= PRICE_MAX:
            reasons.append("PRICE")
        if fee is None:
            reasons.append("FEE_UNVERIFIED")
        if net_edge is None or net_edge < NET_EDGE_MIN:
            reasons.append("NET_EDGE")
        spread = _float(row.get("spread"))
        if spread is None or spread > SPREAD_MAX:
            reasons.append("SPREAD")
        if visible_notional < VISIBLE_NOTIONAL_MIN:
            reasons.append("DEPTH")
        evaluated.append({
            **row,
            "timezone": timezone_name,
            "entry_window_start": iso(window_start) if window_start else None,
            "entry_window_end": iso(window_end) if window_end else None,
            "entry_price_with_impact": entry_price,
            "fee_per_share": fee,
            "gross_edge": gross_edge,
            "net_edge": net_edge,
            "visible_notional": visible_notional,
            "reasons": reasons,
            "passes_market_gates": not reasons,
        })
    current = [row for row in evaluated if row["passes_market_gates"]]
    current.sort(key=lambda row: (row["net_edge"], row["model_probability"]), reverse=True)
    gate = _profitability_gate(monitor.output)
    if not current:
        decision = "NO_ENTRY"
        reason = "Ningún mercado pasa simultáneamente horario, contrato, modelo, precio, comisión, spread y profundidad."
    elif not gate.get("passed"):
        decision = "PAPER_ONLY"
        reason = "Existe señal técnica, pero la muestra oficial acumulada aún no demuestra rentabilidad."
    else:
        decision = "MANUAL_REVIEW"
        reason = "La señal pasa filtros; todavía debe revisar manualmente el contrato y colocar una orden límite."
    highest = evaluated[0] if evaluated else None
    quality_blockers = {
        "TIMEZONE_UNAVAILABLE", "NOT_D1", "SAMPLE_NOT_STRONG", "STATION_GATE", "CONTRACT", "PROBABILITY"
    }
    quality_d1 = [row for row in evaluated if not quality_blockers.intersection(row["reasons"])]
    quality_d1.sort(key=lambda row: row["top_probability"], reverse=True)
    return {
        "schema": SCHEMA,
        "generated_at": iso(observed_at),
        "decision": decision,
        "reason": reason,
        "automatic_orders": False,
        "real_money_allowed": bool(current) and bool(gate.get("passed")),
        "profitability_gate": gate,
        "highest_probability": highest,
        "highest_quality_d1_probability": quality_d1[0] if quality_d1 else None,
        "current_candidates": current,
        "evaluated": evaluated,
        "parameters": {
            "entry": "15:00 station-local D-1, 70-minute window",
            "probability_min": PROBABILITY_MIN,
            "net_edge_min": NET_EDGE_MIN,
            "adverse_price_impact": ADVERSE_PRICE_IMPACT,
            "price_range": [PRICE_MIN, PRICE_MAX],
            "spread_max": SPREAD_MAX,
            "visible_notional_min": VISIBLE_NOTIONAL_MIN,
            "stake_usdc_max": STAKE_USDC,
        },
    }


def _pct(value: Any) -> str:
    return "NO EVALUABLE" if value is None else f"{100 * float(value):.1f}%"


def render_live_ticket(ticket: dict[str, Any]) -> str:
    highest = ticket.get("highest_probability") or {}
    highest_quality = ticket.get("highest_quality_d1_probability") or {}
    current = ticket.get("current_candidates") or []
    top = current[0] if current else None
    top_text = "Ninguna."
    if top:
        top_text = (
            f"{top['city']} {top['market_type']} — YES {top['top_bucket']}; "
            f"probabilidad {_pct(top['top_probability'])}, entrada máxima {float(top['entry_price_with_impact']):.3f}, "
            f"ventaja neta {_pct(top['net_edge'])}."
        )
    highest_text = "NO EVALUABLE"
    if highest:
        highest_text = f"{highest['city']} {highest['market_type']} — {highest['top_bucket']}: {_pct(highest['top_probability'])}"
    quality_text = "NO EVALUABLE"
    if highest_quality:
        quality_text = (
            f"{highest_quality['city']} {highest_quality['market_type']} — "
            f"{highest_quality['top_bucket']}: {_pct(highest_quality['top_probability'])}"
        )
    return f"""# SEÑAL CLIMA V002

Actualizado: {ticket['generated_at']}

## Decisión actual

**{ticket['decision']}** — {ticket['reason']}

- Mejor entrada vigente: {top_text}
- Señales vigentes: {len(current)}
- Órdenes automáticas: desactivadas
- Dinero real autorizado: {'sí' if ticket['real_money_allowed'] else 'no'}

## Probabilidades del ciclo

- Mayor probabilidad D+1 con calidad mínima: {quality_text}
- Mayor probabilidad bruta, aunque no sea operable: {highest_text}

La probabilidad más alta no equivale a la mejor compra. La entrada exige ventaja neta mínima de 5 puntos después de comisión y un centavo de impacto, además de contrato, horario, spread y profundidad válidos.

## Regla manual

Cuando aparezca `MANUAL_REVIEW`, comprobar el contrato visible en Polymarket y colocar personalmente una orden límite que no supere el precio indicado. Máximo V002: 25 USDC por evento. `PAPER_ONLY` y `NO_ENTRY` significan no usar dinero real.
"""


def write_live_ticket(ticket: dict[str, Any], output: Path) -> None:
    json_path = output / "signal_latest.json"
    # Keep the one-click artifact name ASCII-only for reliable Windows batch use.
    markdown_path = output / "SENAL_CLIMA_LATEST.md"
    write_json(json_path, ticket)
    markdown_path.write_text(render_live_ticket(ticket), encoding="utf-8")
    write_json(output / "signal_manifest.json", {
        "schema": SCHEMA,
        "generated_at": ticket["generated_at"],
        "files": [
            {"path": path.name, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in (json_path, markdown_path)
        ],
    })


def _once(database: Path, research_root: Path, output: Path, workers: int) -> dict[str, Any]:
    with MonitorLock(database):
        monitor = ClimateLiveMonitorV2(database, research_root, output, workers=workers)
        run_id = monitor.store.start_run(utc_now() + timedelta(minutes=10), DEFAULT_POLL_SECONDS, DEFAULT_FORECAST_REFRESH_SECONDS)
        try:
            result = monitor.cycle(run_id, 0.0)
            monitor.store.finish_run(run_id, "COMPLETE" if result["status"] == "COMPLETE" else "ERROR", result.get("error"))
            return result
        finally:
            monitor.close()


def _run(database: Path, research_root: Path, output: Path, workers: int, hours: float, poll_seconds: float) -> dict[str, Any]:
    if hours <= 0 or poll_seconds < 60:
        raise ClimateShadowError("hours debe ser positivo y poll-seconds al menos 60")
    end_at = utc_now() + timedelta(hours=hours)
    stop_file = database.resolve().with_suffix(database.suffix + ".stop")
    if stop_file.exists():
        stop_file.unlink()
    with MonitorLock(database):
        monitor = ClimateLiveMonitorV2(database, research_root, output, workers=workers)
        run_id = monitor.store.start_run(end_at, poll_seconds, DEFAULT_FORECAST_REFRESH_SECONDS)
        next_cycle = time.monotonic()
        cycles = errors = 0
        try:
            while utc_now() < end_at and not stop_file.exists():
                result = monitor.cycle(run_id, DEFAULT_FORECAST_REFRESH_SECONDS)
                print(stable_json(result), flush=True)
                cycles += 1
                errors += int(result.get("status") != "COMPLETE")
                next_cycle += poll_seconds
                delay = max(0.0, next_cycle - time.monotonic())
                time.sleep(min(delay, max(0.0, (end_at - utc_now()).total_seconds())))
            status = "STOPPED_BY_MARKER" if stop_file.exists() else "COMPLETE"
            monitor.store.finish_run(run_id, status)
            return {"run_id": run_id, "status": status, "cycles": cycles, "errors": errors}
        finally:
            monitor.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clima V002 dinámico, receive-only y entrada siempre manual")
    parser.add_argument("command", choices=("once", "run", "status", "stop", "finalize"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--research-root", type=Path, default=DEFAULT_RESEARCH_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--poll-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)
    if args.command == "once":
        result = _once(args.database, args.research_root, args.output, args.workers)
    elif args.command == "run":
        result = _run(args.database, args.research_root, args.output, args.workers, args.hours, args.poll_seconds)
    elif args.command == "status":
        result = monitor_status(args.database)
        signal = args.output / "signal_latest.json"
        if signal.exists():
            latest = json.loads(signal.read_text(encoding="utf-8"))
            result["manual_ticket"] = {key: latest.get(key) for key in ("generated_at", "decision", "reason", "real_money_allowed")}
    elif args.command == "stop":
        result = create_stop_marker(args.database)
    else:
        result = write_final_summary(args.database)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

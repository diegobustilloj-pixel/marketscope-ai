from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

from .climate_research import iso, utc_now
from .climate_shadow_monitor import monitor_status as climate_status
from .climate_shadow_monitor import write_final_summary as climate_finalize
from .elon_shadow_monitor import monitor_status as elon_status


SCHEMA = "combined_climate_elon_shadow_close_v001"
DEFAULT_CLIMATE_DB = Path("data/climate_shadow_forward_v001.db")
DEFAULT_ELON_DB = Path("data/elon_shadow_forward_v001.db")
DEFAULT_OUTPUT = Path("data/combined_shadow_close_20260902_1949_BOL")


def _finished(status: dict[str, Any]) -> bool:
    run = status.get("latest_run") or {}
    return run.get("status") not in (None, "RUNNING")


def _wait_for_close(climate_db: Path, elon_db: Path, seconds: float) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        climate = climate_status(climate_db)
        elon = elon_status(elon_db)
        if _finished(climate) and _finished(elon):
            return climate, elon
        if time.monotonic() >= deadline:
            return climate, elon
        time.sleep(min(10.0, deadline - time.monotonic()))


def _elon_summary(database: Path, status: dict[str, Any]) -> dict[str, Any]:
    db = sqlite3.connect(database.resolve())
    db.row_factory = sqlite3.Row
    totals = dict(db.execute(
        """SELECT COUNT(*) cycles,COALESCE(SUM(status='COMPLETE'),0) complete_cycles,
        COALESCE(SUM(status='ERROR'),0) error_cycles,COALESCE(SUM(quotes),0) quotes,
        COALESCE(SUM(decisions),0) decisions,COALESCE(SUM(signals),0) new_signals,
        COALESCE(SUM(new_posts),0) new_posts FROM shadow_cycles"""
    ).fetchone())
    signal_totals = dict(db.execute(
        """SELECT COUNT(*) signals,COALESCE(SUM(status='RESOLVED_SHADOW'),0) resolved,
        COALESCE(SUM(status='OPEN_SHADOW'),0) open,COALESCE(SUM(CASE WHEN pnl_usdc>0 THEN 1 ELSE 0 END),0) wins,
        COALESCE(SUM(CASE WHEN pnl_usdc<0 THEN 1 ELSE 0 END),0) losses,
        COALESCE(SUM(pnl_usdc),0.0) net_pnl_usdc,AVG(CASE WHEN status='RESOLVED_SHADOW' THEN pnl_usdc END) ev_resolved_usdc,
        AVG(net_edge) average_signal_net_edge,AVG(expected_roi) average_expected_roi FROM shadow_signals"""
    ).fetchone())
    by_threshold = [dict(row) for row in db.execute(
        """SELECT threshold,COUNT(*) signals,SUM(status='RESOLVED_SHADOW') resolved,
        SUM(CASE WHEN pnl_usdc>0 THEN 1 ELSE 0 END) wins,SUM(CASE WHEN pnl_usdc<0 THEN 1 ELSE 0 END) losses,
        SUM(COALESCE(pnl_usdc,0)) net_pnl_usdc,AVG(net_edge) average_net_edge
        FROM shadow_signals GROUP BY threshold ORDER BY threshold"""
    )]
    by_market = [dict(row) for row in db.execute(
        """SELECT slug,COUNT(*) signals,SUM(status='RESOLVED_SHADOW') resolved,
        SUM(COALESCE(pnl_usdc,0)) net_pnl_usdc,AVG(net_edge) average_net_edge,MAX(window_end) window_end
        FROM shadow_signals GROUP BY slug ORDER BY signals DESC"""
    )]
    db.close()
    resolved = int(signal_totals["resolved"] or 0)
    wins = int(signal_totals["wins"] or 0)
    signal_totals["resolved_win_rate"] = wins / resolved if resolved else None
    return {
        "status": status,
        "totals": totals,
        "signal_totals": signal_totals,
        "by_threshold": by_threshold,
        "by_market": by_market,
        "real_money_allowed": False,
    }


def _fmt(value: Any, digits: int = 3) -> str:
    return "NO EVALUABLE" if value is None else f"{float(value):.{digits}f}"


def _pct(value: Any, digits: int = 1) -> str:
    return "NO EVALUABLE" if value is None else f"{100.0 * float(value):.{digits}f}%"


def _render(payload: dict[str, Any]) -> str:
    climate = payload["climate"]
    elon = payload["elon"]
    ct = climate["totals"]
    et = elon["totals"]
    es = elon["signal_totals"]
    climate_run = climate.get("latest_run") or {}
    elon_run = (elon.get("status") or {}).get("latest_run") or {}
    top_climate = climate.get("top_edge_candidates") or []
    top_line = "Ninguna oportunidad con ask ejecutable."
    if top_climate:
        top = top_climate[0]
        top_line = (
            f"{top['city']} {top['market_type']} / {top['station_id']} / {top['bucket']} {top['side']}: "
            f"edge bruto observado {_pct(top['gross_edge'])}; decisión {top['decision']} ({top['reason']})."
        )
    return f"""# CIERRE SHADOW — CLIMA + TWEETS DE ELON

Fecha de cierre solicitada: 2026-09-02 19:49 Bolivia.

Generado: {payload['generated_at']}.

## Resultado ejecutivo

- Clima: collector {climate_run.get('status', 'UNKNOWN')}; dinero real bloqueado.
- Tuits de Elon: monitor {elon_run.get('status', 'UNKNOWN')}; dinero real bloqueado.
- La finalización de la ventana de captura no implica que todos los mercados estén resueltos. Los resultados económicos solo usan señales ya resueltas.

## Clima

- Ciclos: {ct['cycles']} ({ct['complete_cycles']} completos; {ct['error_cycles']} con error).
- Forecasts calibrados almacenados: {ct['forecast_rows']}.
- Observaciones METAR nuevas: {ct['new_observations']}.
- Cotizaciones YES/NO: {ct['quote_rows']}.
- Decisiones por threshold: {ct['decision_rows']}.
- Candidatos que superaron edge bruto: {ct['edge_candidates']}.
- Cobertura de ask: {_pct(climate.get('book_coverage'))}.
- Cobertura de mercados con probabilidad: {_pct(climate.get('forecast_market_coverage'))}.
- Contratos estrictos: {climate.get('strict_contract_markets', 0)}.
- Mejor candidato observado: {top_line}

Interpretación: los candidatos climáticos son contrafactuales y quedan bloqueados cuando timezone/frecuencia no están explícitas. No equivalen a trades ni a beneficio.

## Tuits de Elon

- Ciclos: {et['cycles']} ({et['complete_cycles']} completos; {et['error_cycles']} con error).
- Posts nuevos durante la corrida: {et['new_posts']}.
- Cotizaciones: {et['quotes']}.
- Decisiones: {et['decisions']}.
- Señales shadow únicas: {es['signals']}.
- Resueltas: {es['resolved']}; abiertas: {es['open']}.
- Wins/Losses resueltos: {es['wins']}/{es['losses']}.
- Win rate resuelto: {_pct(es['resolved_win_rate'])}.
- PnL shadow resuelto: {_fmt(es['net_pnl_usdc'], 2)} USDC.
- EV por señal resuelta: {_fmt(es['ev_resolved_usdc'], 2)} USDC.
- Edge neto medio modelado al emitir señal: {_pct(es['average_signal_net_edge'])}.

Interpretación: si quedan señales abiertas, ROI, profit factor y drawdown definitivos continúan siendo NO EVALUABLE hasta sus resoluciones.

## Veredicto

Clima: continuar con shadow; NO activar capital real.

Elon: decidir únicamente con las señales resueltas. Si la muestra resuelta sigue incompleta, continuar como NO EVALUABLE y no extrapolar las señales abiertas.

Capital real autorizado: $0.
"""


def build_combined_report(
    climate_db: Path = DEFAULT_CLIMATE_DB,
    elon_db: Path = DEFAULT_ELON_DB,
    output: Path = DEFAULT_OUTPUT,
    *,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    climate_state, elon_state = _wait_for_close(climate_db, elon_db, wait_seconds)
    climate = climate_finalize(climate_db)
    elon = _elon_summary(elon_db, elon_state)
    payload = {
        "schema": SCHEMA,
        "generated_at": iso(utc_now()),
        "requested_close": "2026-09-02T19:49:00-04:00",
        "climate": climate,
        "elon": elon,
        "real_money_allowed": False,
    }
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path = output.with_suffix(".json")
    markdown_path = output.with_suffix(".md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render(payload), encoding="utf-8")
    manifest = {
        "schema": SCHEMA,
        "generated_at": payload["generated_at"],
        "files": [
            {"path": path.name, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in (json_path, markdown_path)
        ],
    }
    output.with_name(output.name + "_manifest").with_suffix(".json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cierra y consolida los shadow de Clima y Elon sin dinero real")
    parser.add_argument("--climate-db", type=Path, default=DEFAULT_CLIMATE_DB)
    parser.add_argument("--elon-db", type=Path, default=DEFAULT_ELON_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--wait-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)
    result = build_combined_report(
        args.climate_db, args.elon_db, args.output, wait_seconds=args.wait_seconds
    )
    print(json.dumps({
        "generated_at": result["generated_at"],
        "climate_status": (result["climate"].get("latest_run") or {}).get("status"),
        "elon_status": ((result["elon"].get("status") or {}).get("latest_run") or {}).get("status"),
        "real_money_allowed": result["real_money_allowed"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

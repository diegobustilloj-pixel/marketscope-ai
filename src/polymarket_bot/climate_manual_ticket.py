from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .climate_research import iso, stable_json, utc_now, write_json


SCHEMA = "climate_manual_ticket_v001"
DEFAULT_REPORT = Path("data/climate_manual_strategy_v001/backtest_report.json")
DEFAULT_OUTPUT = Path("data/climate_manual_strategy_v001")


def _as_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def select_ticket(report: dict[str, Any], now: datetime) -> dict[str, Any]:
    gate = report["profitability_gate"]
    audit = report["primary_provisional_diagnostic"]["audit"]
    candidates = []
    for row in audit:
        reasons = {reason for reason in str(row.get("reasons") or "").split(",") if reason}
        if reasons != {"OUTCOME_OPEN_OR_AMBIGUOUS"}:
            continue
        deadline = row.get("entry_window_end")
        if not deadline or now > _as_utc(str(deadline)):
            continue
        candidates.append(row)
    candidates.sort(key=lambda row: (float(row.get("model_probability") or 0), float(row.get("net_edge_after_fee") or 0)), reverse=True)
    if not candidates:
        decision = "NO_ENTRY"
        reason = "No hay señal abierta que cumpla todos los filtros dentro de su ventana horaria."
    elif not gate.get("passed"):
        decision = "PAPER_ONLY"
        reason = "Hay una señal técnica, pero el backtest aún no aprobó la puerta de rentabilidad."
    else:
        decision = "MANUAL_REVIEW"
        reason = "Revise manualmente contrato, estación, fuente y orden límite antes de decidir."
    return {
        "schema": SCHEMA,
        "generated_at": iso(now),
        "decision": decision,
        "reason": reason,
        "real_money_allowed": bool(gate.get("passed")) and bool(candidates),
        "automatic_orders": False,
        "highest_probability_observed": report.get("highest_probability_observed", {}),
        "candidate_count": len(candidates),
        "top_candidate": candidates[0] if candidates else None,
        "candidates": candidates,
        "profitability_gate": gate,
    }


def _pct(value: Any) -> str:
    return "NO EVALUABLE" if value is None else f"{100 * float(value):.1f}%"


def render_ticket(ticket: dict[str, Any]) -> str:
    highest = ticket.get("highest_probability_observed") or {}
    top = ticket.get("top_candidate")
    candidate_text = "Ninguna."
    if top:
        candidate_text = (
            f"{top['city']} {top['market_type']} — YES {top['bucket']}; "
            f"probabilidad {_pct(top['model_probability'])}, precio con impacto {float(top['entry_price']):.3f}, "
            f"ventaja neta {_pct(top['net_edge_after_fee'])}."
        )
    return f"""# TICKET MANUAL — POLYMARKET CLIMA V001

Generado: {ticket['generated_at']}

## Decisión

**{ticket['decision']}** — {ticket['reason']}

- Órdenes automáticas: desactivadas.
- Dinero real permitido por el sistema: {'sí' if ticket['real_money_allowed'] else 'no'}.
- Señales vigentes que pasan filtros: {ticket['candidate_count']}.
- Mejor señal vigente: {candidate_text}

## Mayor probabilidad observada en la muestra

{highest.get('city')} {highest.get('market_type')} — {highest.get('top_bucket')}: {_pct(highest.get('top_probability'))}.

Es una probabilidad del modelo, no una recomendación de compra. Una probabilidad alta puede ser una mala entrada si el precio es todavía más alto o si el contrato no coincide con la estación meteorológica modelada.

## Acción humana obligatoria

Solo cuando la decisión sea `MANUAL_REVIEW`: comprobar en Polymarket el texto de resolución, estación/fuente, zona horaria y frecuencia; después colocar personalmente una orden límite. Nunca usar orden de mercado ni superar 25 USDC por evento bajo V001.
"""


def run(report_path: Path, output: Path) -> dict[str, Any]:
    report_path = report_path.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    ticket = select_ticket(report, utc_now())
    json_path = output / "manual_ticket_latest.json"
    markdown_path = output / "TICKET_MANUAL_LATEST.md"
    write_json(json_path, ticket)
    markdown_path.write_text(render_ticket(ticket), encoding="utf-8")
    manifest = {
        "schema": SCHEMA,
        "generated_at": ticket["generated_at"],
        "inputs": {"backtest_report": str(report_path)},
        "outputs": [
            {"path": path.name, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in (json_path, markdown_path)
        ],
    }
    write_json(output / "manual_ticket_manifest.json", manifest)
    return ticket


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ticket semiautomático de clima; nunca envía órdenes")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    ticket = run(args.report, args.output)
    print(stable_json({
        "decision": ticket["decision"],
        "reason": ticket["reason"],
        "candidate_count": ticket["candidate_count"],
        "real_money_allowed": ticket["real_money_allowed"],
        "highest_probability_observed": ticket["highest_probability_observed"],
        "top_candidate": ticket["top_candidate"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

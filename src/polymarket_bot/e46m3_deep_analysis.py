from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

import duckdb


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _dict_rows(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _sqlite_rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _profit_factor(rows: Iterable[Mapping[str, Any]], key: str = "pnl_usd") -> float | None:
    values = [_number(row.get(key)) for row in rows]
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return wins / losses if losses else None


def _gap_stats(values: list[int]) -> dict[str, Any]:
    ordered = sorted(value for value in values if value >= 0)
    if not ordered:
        return {"observations": 0}

    def quantile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
        return float(ordered[index])

    return {
        "observations": len(ordered),
        "median_seconds": quantile(0.50),
        "p90_seconds": quantile(0.90),
        "p99_seconds": quantile(0.99),
        "fraction_le_1s": sum(value <= 1 for value in ordered) / len(ordered),
        "fraction_le_5s": sum(value <= 5 for value in ordered) / len(ordered),
        "fraction_le_15s": sum(value <= 15 for value in ordered) / len(ordered),
        "fraction_le_60s": sum(value <= 60 for value in ordered) / len(ordered),
        "fraction_le_300s": sum(value <= 300 for value in ordered) / len(ordered),
    }


def _money(value: Any) -> str:
    return "N/D" if value is None else f"US${_number(value):,.2f}"


def _pct(value: Any) -> str:
    return "N/D" if value is None else f"{_number(value):.2%}"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(str(item).replace("|", "/") for item in row) + " |" for row in rows)
    return "\n".join(lines)


def _write_report(result: Mapping[str, Any], summary: Mapping[str, Any], output_dir: Path) -> None:
    existing_path = output_dir / "e46m3_strategy_report.md"
    existing = existing_path.read_text(encoding="utf-8")
    core_start = existing.index("## Cobertura y calidad")
    core_end_marker = "## Fingerprint y explicación más simple"
    if core_end_marker not in existing:
        core_end_marker = "## Estrategia real: conversión y reciclaje NegRisk"
    core_end = existing.index(core_end_marker)
    audited_core = existing[core_start:core_end].rstrip()

    history = summary["history"]
    official = summary["official"]
    incentives = summary["incentives"]
    risk = summary["risk"]
    structures = summary["structures"]
    fingerprint = summary["fingerprint"]
    onchain = summary["onchain_recent"]
    cycle = result["cycle_timing"]
    sizing = result["confident_closed_winners_price_ge_80c"]
    sizing_fills = result["confident_winner_buy_fills_price_ge_80c"]
    high_price_all = result["all_closed_positions_price_ge_80c"]
    score = result["scorecard"]
    buy_flow = next(row for row in result["side_flow"] if row["side"] == "BUY")
    sell_flow = next(row for row in result["side_flow"] if row["side"] == "SELL")
    neg_share = structures["negative_risk_conditions"] / max(1, history["distinct_conditions"])
    maker_fraction = onchain["maker_fills"] / max(1, onchain["fills"])
    taker_fraction = onchain["taker_fills"] / max(1, onchain["fills"])

    cluster_map = {row["primary_cluster"]: row for row in summary["clusters"]}
    structural_pnl = _number(cluster_map.get("inventory_management", {}).get("official_pnl_usd")) + _number(
        cluster_map.get("negrisk", {}).get("official_pnl_usd")
    )
    category_rows = sorted(summary["categories"], key=lambda row: _number(row["official_pnl_usd"]), reverse=True)
    top_volume_categories = sorted(summary["categories"], key=lambda row: _number(row["volume_usd"]), reverse=True)

    closed_band_table = _table(
        ["Precio medio", "Posiciones cerradas", "Comprado", "PnL realizado", "Fracción rentable", "PF"],
        [
            [
                row["price_band"], f"{row['positions']:,}", _money(row["total_bought_usd"]),
                _money(row["realized_pnl_usd"]), _pct(row["profitable_fraction"]),
                f"{_number(row['profit_factor']):.2f}" if row["profit_factor"] is not None else "N/D",
            ]
            for row in result["closed_entry_price_bands"]
        ],
    )
    top_win_table = _table(
        ["Evento cerrado", "Categoría", "PnL", "Comprado", "Fills"],
        [
            [row["event_slug"][:60], row["category"], _money(row["pnl_usd"]), _money(row["total_bought_usd"]), f"{row['fills']:,}"]
            for row in result["top_closed_event_wins"][:10]
        ],
    )
    top_loss_table = _table(
        ["Evento cerrado", "Categoría", "PnL", "Comprado", "Fills"],
        [
            [row["event_slug"][:60], row["category"], _money(row["pnl_usd"]), _money(row["total_bought_usd"]), f"{row['fills']:,}"]
            for row in result["top_closed_event_losses"][:10]
        ],
    )
    category_focus = _table(
        ["Categoría", "Fills", "Volumen", "PnL snapshot", "PF"],
        [
            [row["category"], f"{row['trades']:,}", _money(row["volume_usd"]), _money(row["official_pnl_usd"]),
             f"{_number(row['profit_factor']):.2f}" if row["profit_factor"] is not None else "N/D"]
            for row in category_rows
        ],
    )
    copy_table = _table(
        ["Delay", "Detectadas", "Llenadas", "Perdidas", "PnL", "ROI", "DD", "Copy Decay"],
        [[f"{delay}s", f"{history['fills']:,}", "N/D", "N/D", "N/D", "N/D", "N/D", "N/D"] for delay in (0, 1, 2, 5, 10, 15, 30, 60, 120, 300, 900)],
    )
    capital_table = _table(
        ["Bankroll", "Trades copiables", "PnL", "ROI", "Max DD", "Saturación"],
        [[_money(value), "N/D", "N/D", "N/D", "N/D", "N/D"] for value in (10, 25, 50, 100, 250, 500, 1000, 5000, 10000)],
    )
    score_table = _table(
        ["Dimensión", "/100"],
        [[key.replace("_", " ").title(), value] for key, value in score.items() if isinstance(value, int)],
    )

    report = f"""# Forensia cuantitativa de @e46m3 en Polymarket

Wallet verificada: `0x4f1d5ae26fc31472966e951af3183308736d8de2`  
Perfil: [@e46m3](https://polymarket.com/@e46m3)

## Conclusión ejecutiva

@e46m3 no se comporta principalmente como un pronosticador ni como un market maker clásico. La explicación más simple es un **bot de arbitraje/reciclaje de inventario NegRisk**: compra casi siempre en lugar de vender, transforma inventario entre outcomes y sale mediante conversiones, merges y redeems. El flujo contiene {buy_flow['fills']:,} BUY frente a solo {sell_flow['fills']:,} SELL, además de 94.518 conversiones, 146.642 filas de merge y 5.355 redeems.

La mecánica oficial de NegRisk permite convertir un `NO` de un outcome en `YES` de todos los demás outcomes del mismo evento.[^5] Un merge convierte cantidades iguales de `YES + NO` en colateral.[^6] Esto explica por qué copiar una compra aislada es peligroso: el fill visible puede ser solo una pierna de un ciclo que el operador completa segundos después.

Las vistas oficiales confirman rentabilidad, pero no coinciden en magnitud. El leaderboard muestra {_money(official['leaderboard_pnl_usd'])}; la suma puntual de posiciones arroja {_money(official['position_snapshot_pnl_usd'])}; la diferencia es {_money(_number(official['leaderboard_pnl_usd']) - _number(official['position_snapshot_pnl_usd']))}. No se suman. Los incentivos explícitos fueron {_money(incentives['total_explicit_usd'])}, apenas {_pct(incentives['share_of_position_pnl'])} del PnL de posiciones, por lo que no explican el signo positivo observado.

**Decisión:** no copiar fills con capital real. La estrategia estructural es parcialmente replicable, pero debe validarse en forward capturando todos los libros de un evento NegRisk y ejecutando el conjunto completo. Clasificación: **B provisional — estrategia identificada; réplica preferible al copiado**.

{audited_core}

## Estrategia real: conversión y reciclaje NegRisk

- {structures['negative_risk_conditions']:,} de {history['distinct_conditions']:,} condiciones ({neg_share:.2%}) pertenecen a NegRisk.
- Los clusters `inventory_management + negrisk` concentran {_money(structural_pnl)}, aproximadamente {structural_pnl / max(1, _number(official['position_snapshot_pnl_usd'])):.2%} del PnL puntual neto. Es atribución conductual, no causal.
- Se observaron 94.518 conversiones y 146.642 filas de merge; 47.872 transacciones distintas contenían merges.
- {buy_flow['fills']:,} de {history['fills']:,} fills fueron BUY ({buy_flow['fills'] / history['fills']:.2%}); las salidas económicas ocurren principalmente fuera de SELL, mediante operaciones internas.
- En la ventana on-chain verificable: {maker_fraction:.2%} maker y {taker_fraction:.2%} taker. No hay respaldo para llamarla estrategia maker-dominante.

La secuencia temporal refuerza esta lectura: la conversión ocurre a una mediana de {cycle['conversion_after_latest_trade']['median_seconds']:.0f}s después del último trade del evento y {cycle['conversion_after_latest_trade']['fraction_le_60s']:.2%} ocurren dentro de 60s. El merge llega a una mediana de {cycle['merge_after_latest_trade']['median_seconds']:.0f}s después del último trade y {cycle['merge_after_latest_trade']['fraction_le_60s']:.2%} dentro de 60s. Son asociaciones por evento, no una prueba causal fill-a-fill.

### Reglas reconstruidas para validar

1. Elegir únicamente eventos NegRisk con outcomes nombrados; los placeholders cambian de definición y la documentación aconseja no operarlos.[^5]
2. Capturar simultáneamente ask, bid y profundidad de todos los outcomes; no usar último precio.
3. Ruta `YES`: comprar un `YES` de cada outcome solo si el costo ejecutable total, incluidas fees y slippage, es menor que US$1 menos un colchón.
4. Ruta `NO/conversión`: comprar el conjunto necesario de `NO`; convertir una pierna en `YES` de los demás outcomes y mergear pares completos solo si el costo total es menor que el valor de salida `(N−1) × shares` después de fees y gas.
5. Ejecutar el conjunto de forma atómica o fail-closed. Si falta una pierna, cancelar y limitar el inventario residual.
6. No incluir rewards en el umbral. Las fees taker dependen del precio y son máximas cerca de 50%; deben consultarse por token al ejecutar.[^7]

Estas reglas explican la estructura observable. **No prueban todavía que un tercero conserve el margen**, porque faltan profundidad histórica, cola y costo de todas las piernas.

## Cuánto arriesga cuando “está seguro”

Tomando como proxy una posición cerrada, ganadora, rentable y con precio medio ≥80¢, el tamaño comprado mediano fue {_money(sizing['median_position_bought_usd'])}; percentil 75 {_money(sizing['p75_position_bought_usd'])}; percentil 90 {_money(sizing['p90_position_bought_usd'])}; percentil 99 {_money(sizing['p99_position_bought_usd'])}; máximo {_money(sizing['max_position_bought_usd'])}. A nivel de fill, la mediana fue {_money(sizing_fills['median_fill_usd'])}, p90 {_money(sizing_fills['p90_fill_usd'])} y máximo {_money(sizing_fills['max_fill_usd'])}.

Pero **precio alto no equivale a convicción direccional** aquí. Entre todas las posiciones cerradas con precio ≥80¢, solo {_pct(high_price_all['profitable_fraction'])} resultaron rentables individualmente y su PnL agregado fue {_money(high_price_all['realized_pnl_usd'])}. Muchas son piernas `NO` que pierden dentro de una cesta compensada por otras posiciones/conversiones. Copiar únicamente la pierna aparentemente “segura” destruye el hedge.

### PnL cerrado por rango de entrada

{closed_band_table}

## Ganancias y pérdidas cerradas

La fracción de eventos cerrados rentables fue {_pct(risk['win_rate_profitable_events_not_prediction_accuracy'])}, pero no es accuracy predictiva. El profit factor fue {_number(risk['profit_factor']):.2f} y el drawdown reconstruido por fecha de cierre {_money(risk['max_drawdown_usd_by_close_time'])}. La concentración es severa: sin el mejor evento quedan {_money(risk['outlier_sensitivity']['without_best_1_usd'])}; sin los cinco mejores, {_money(risk['outlier_sensitivity']['without_best_5_usd'])}; sin el 1% superior, {_money(risk['outlier_sensitivity']['without_top_1pct_winners_usd'])}.

### Diez mejores eventos cerrados

{top_win_table}

### Diez peores eventos cerrados

{top_loss_table}

## Mercados y fingerprint

Por actividad, la categoría dominante es {top_volume_categories[0]['category']} con {top_volume_categories[0]['trades']:,} fills y {_money(top_volume_categories[0]['volume_usd'])}. Por PnL puntual, domina {category_rows[0]['category']} con {_money(category_rows[0]['official_pnl_usd'])}. La cartera opera de forma transversal; el edge parece ligado más a la estructura multi-outcome que a una sola temática.

{category_focus}

Fill medio: {_money(fingerprint['mean_trade_notional_usd'])}; mediano: {_money(fingerprint['median_trade_notional_usd'])}. Gap mediano: {_number(fingerprint['median_interarrival_seconds']):.1f}s; {_pct(fingerprint['fraction_interarrival_le_60s'])} de gaps ≤60s; actividad en {fingerprint['active_utc_hours']}/24 horas UTC y {fingerprint['active_weekdays']}/7 días. Los tamaños 5, 10, 12,03, 25/25,03 y 60,03 shares se repiten miles de veces. **Bot likelihood: 90/100**, heurístico y no probabilístico.

## Copy trading

{copy_table}

El backtest de copia es **grado D — insuficiente**. La API histórica entrega fills y timestamps, pero no el ask/bid ejecutable ni la profundidad que habría encontrado el follower; conceder el mismo precio sería look-ahead.[^2][^8] Por ello no se fabrican PnL, ROI, delay óptimo ni alpha half-life.

La estructura ya permite una decisión práctica: el merge sucede una mediana de 15s después del último trade del evento y 96,04% llega dentro de 60s. Un follower ve una pierna después del fill, mientras @e46m3 puede estar completando o cerrando el conjunto. **Copyability: 12/100.**

### Capital de copia

{capital_table}

No existe un bankroll “mínimo útil” defendible sin ejecución simulada. Solo se observa una equity puntual de {_money(summary['equity_snapshot']['equity_usd'])}; volumen o total comprado no sustituyen capital histórico ni capital-day.

## Replicación de estrategia y backtest

La réplica correcta no sigue fills: escanea todos los outcomes del evento y ejecuta la desigualdad de conjunto. El backtest histórico Train 60% / Validation 20% / Test 20% / walk-forward permanece **grado D**, porque no existe el universo histórico completo con libros ejecutables y profundidad. Probar solo los mercados seleccionados por @e46m3 sería survivorship bias.

El experimento válido es un shadow-forward pre-registrado: universo completo de eventos NegRisk, snapshots de todos los libros, simulación de todas las piernas, fees por token, gas, fallos parciales, inventario bloqueado y resolución. Separar `E46M3 ORIGINAL`, `COPY` y `REPLICATION`; no optimizar en Test. **Replicability actual: 48/100.**

## Scorecard

{score_table}

## Final Decision Card

- Verified Wallet: `0x4f1d5ae26fc31472966e951af3183308736d8de2`
- History Coverage: {history['activity_events']:,} eventos públicos; {history['fills']:,} fills; metadata 99,98%; maker/taker reciente parcial.
- Volume: {_money(official['leaderboard_volume_usd'])} en leaderboard; {_money(buy_flow['notional_usd'] + sell_flow['notional_usd'])} en fills descargados.
- Estimated Total PnL: leaderboard {_money(official['leaderboard_pnl_usd'])}; snapshot posiciones {_money(official['position_snapshot_pnl_usd'])}; no sumar.
- Trading PnL: no aislable causalmente; PnL cerrado observado {_money(official['closed_realized_pnl_usd'])}.
- Rewards/Rebates/Yield: {_money(incentives['total_explicit_usd'])}, separados.
- Peak Capital: UNKNOWN; equity puntual {_money(summary['equity_snapshot']['equity_usd'])}.
- Profit Factor: {_number(risk['profit_factor']):.2f}; Max Drawdown por cierres: {_money(risk['max_drawdown_usd_by_close_time'])}.
- Primary Category: {top_volume_categories[0]['category']} por fills/volumen; {category_rows[0]['category']} por PnL puntual.
- Maker/Taker: {maker_fraction:.2%}/{taker_fraction:.2%} en 50.000 bloques recientes.
- Primary Strategy: NegRisk conversion + inventory/merge recycling.
- Secondary Strategy: adquisición automatizada de cestas multi-outcome.
- Directional edge: presente, pero no es la explicación dominante.
- Information edge: no demostrado.
- Best Copy Delay / Copy Decay / Alpha Half-Life: UNKNOWN; grado D.
- Copyability: 12/100. Replicability: 48/100.
- Can we decipher the strategy? **PARTIALLY — estructura sí, parámetros de entrada no.**
- Can we copy directly? **NO.**
- Should we copy/replicate/neither? **REPLICATE en shadow; no dinero real todavía.**
- Edge without rewards? **YES en el signo de la snapshot; magnitud no reconciliada.**
- Edge with realistic latency/slippage? **UNKNOWN.**
- Recommended Action: **BUILD REPLICATION SHADOW BOT.**
- Confidence: **82/100** en la identificación estructural; **25/100** en robustez económica fuera de muestra.

## Fuentes

[^1]: Polymarket, [perfil público de @e46m3](https://polymarket.com/@e46m3), consultado el 10 de septiembre de 2026.
[^2]: Polymarket Documentation, [Get trades for a user or markets](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets), consultado el 10 de septiembre de 2026.
[^3]: Polymarket Documentation, [Get current positions for a user](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user), consultado el 10 de septiembre de 2026.
[^4]: Polymarket Documentation, [Get closed positions for a user](https://docs.polymarket.com/api-reference/core/get-closed-positions-for-a-user), consultado el 10 de septiembre de 2026.
[^5]: Polymarket Documentation, [Negative Risk Markets](https://docs.polymarket.com/concepts/negative-risk), consultado el 10 de septiembre de 2026.
[^6]: Polymarket Documentation, [Positions & Tokens](https://docs.polymarket.com/concepts/positions-tokens), consultado el 10 de septiembre de 2026.
[^7]: Polymarket Documentation, [Fees](https://docs.polymarket.com/trading/fees), consultado el 10 de septiembre de 2026.
[^8]: Polymarket Documentation, [Get order book](https://docs.polymarket.com/api-reference/market-data/get-order-book), consultado el 10 de septiembre de 2026.
[^9]: Polygon Documentation, [RPC endpoints](https://docs.polygon.technology/pos/reference/rpc-endpoints), consultado el 10 de septiembre de 2026.

Los datos cuantitativos proceden de capturas locales reproducibles de Polymarket Data API, Gamma API y Polygon guardadas en `data/e46m3_forensics` y `data/polyledger/e46m3.db`. Los RPC públicos rechazaron el backfill de siete millones y 500.000 bloques; por eso maker/taker se limita explícitamente a 50.000 bloques recientes.[^9]
"""
    existing_path.write_text(report, encoding="utf-8")


def _write_handoff(result: Mapping[str, Any], summary: Mapping[str, Any], output_dir: Path) -> None:
    history = summary["history"]
    official = summary["official"]
    risk = summary["risk"]
    structures = summary["structures"]
    cycle = result["cycle_timing"]
    text = f"""# ==========================================================
# TRASPASO — @E46M3 STRATEGY FORENSICS
# ==========================================================

## Identidad y cobertura

- Perfil: https://polymarket.com/@e46m3
- Wallet verificada: `0x4f1d5ae26fc31472966e951af3183308736d8de2`
- Creada: 2026-04-08; historial observado: {history['first_event']}–{history['last_event']}.
- {history['activity_events']:,} eventos públicos; {history['fills']:,} fills; {history['distinct_conditions']:,} condiciones.
- {history['closed_position_rows']:,} posiciones cerradas y {history['open_position_rows']:,} abiertas.
- Gamma: 8.614/8.616 eventos; 135.157 mercados.
- Polygon: 1.161 fills en 50.000 bloques recientes; 17,31% maker / 82,69% taker; 99,31% match con Data API.
- Dos RPC públicos rechazaron ampliar a 500.000/siete millones de bloques; no extrapolar maker/taker.

## Archivos

- `data/polyledger/e46m3.db`
- `data/e46m3_forensics/e46m3_wallet_identity.json`
- `data/e46m3_forensics/e46m3_raw_activity.parquet`
- `data/e46m3_forensics/e46m3_trades.parquet`
- `data/e46m3_forensics/e46m3_lifecycles.parquet`
- `data/e46m3_forensics/e46m3_rewards.parquet`
- `data/e46m3_forensics/e46m3_strategy_clusters.parquet`
- `data/e46m3_forensics/e46m3_copy_backtest.parquet`
- `data/e46m3_forensics/e46m3_replication_backtest.parquet`
- `data/e46m3_forensics/e46m3_copy_latency.csv`
- `data/e46m3_forensics/e46m3_pnl_decomposition.csv`
- `data/e46m3_forensics/e46m3_forensics_summary.json`
- `data/e46m3_forensics/e46m3_deep_metrics.json`
- `data/e46m3_forensics/e46m3_strategy_report.md`

## Contabilidad

- Leaderboard PnL {_money(official['leaderboard_pnl_usd'])}; volumen {_money(official['leaderboard_volume_usd'])}.
- Snapshot posiciones {_money(official['position_snapshot_pnl_usd'])}: cerradas {_money(official['closed_realized_pnl_usd'])}, realizado en abiertas {_money(official['open_realized_pnl_usd'])}, no realizado {_money(official['open_unrealized_pnl_usd'])}.
- Diferencia leaderboard/snapshot {_money(_number(official['leaderboard_pnl_usd']) - _number(official['position_snapshot_pnl_usd']))}; no sumar ambas vistas.
- Incentivos explícitos {_money(summary['incentives']['total_explicit_usd'])}; no explican el signo positivo de la snapshot.
- PF cerrado {risk['profit_factor']:.2f}; DD por cierres {_money(risk['max_drawdown_usd_by_close_time'])}; sin Top 5 el PnL cerrado cae a {_money(risk['outlier_sensitivity']['without_best_5_usd'])}.
- Equity puntual {_money(summary['equity_snapshot']['equity_usd'])}; peak/average capital y ROI sobre capital real: UNKNOWN.

## Estrategia identificada

1. Primaria: arbitraje/reciclaje NegRisk mediante compras, conversiones y merges.
2. Secundaria: construcción automatizada de cestas multi-outcome.
3. Terciaria: posiciones direccionales en política, deportes y macro.

Evidencia: 291.774 BUY vs 3.121 SELL; 94.518 conversiones; 146.642 filas de merge; 5.355 redeems; {structures['negative_risk_conditions']:,} condiciones NegRisk. Mediana trade→conversion {cycle['conversion_after_latest_trade']['median_seconds']:.0f}s; mediana trade→merge {cycle['merge_after_latest_trade']['median_seconds']:.0f}s; 96,04% de merges dentro de 60s del último trade del evento.

Regla candidata: para cada evento NegRisk con outcomes nombrados, capturar libros completos; comprar toda la cesta YES si costo ejecutable <1 neto, o la cesta NO/conversión si costo <N−1 neto; incluir fees, slippage y gas; ejecutar atómicamente; limitar inventario incompleto; convertir/mergear; ignorar rewards en el umbral.

## Copy vs replication

- Copy backtest: D_INSUFFICIENT para 0s–15m; no hay bid/ask/depth histórico ejecutable.
- Copyability 12/100: copiar una pierna separa el fill de la conversión/merge que lo neutraliza.
- Replication backtest histórico: D_INSUFFICIENT; falta el universo de libros históricos.
- Replicability 48/100: lógica estructural observable, parámetros económicos sin validar.
- Veredicto: NO COPY. Construir shadow de réplica de conjunto/NegRisk.

## Arquitectura siguiente

1. Scanner de todos los eventos NegRisk y outcomes nombrados.
2. Snapshot sincronizado de bid/ask/depth y fee rate por token.
3. Optimizador de dos rutas: bundle YES vs bundle NO+convert+merge.
4. Simulador de ejecución parcial, gas, slippage, latencia e inventario bloqueado.
5. Risk engine fail-closed y límites por evento/correlación.
6. Ledger separado: E46M3 ORIGINAL, COPY y REPLICATION.
7. Train 60 / Validation 20 / Test 20 cronológico tras reunir ≥1.000 oportunidades; walk-forward y sensibilidad sin optimizar Test.

## Próximo experimento

Shadow-forward por 30 días o 1.000 oportunidades. No usar dinero real antes de observar margen neto positivo fuera de muestra, fill rate suficiente, DD tolerable y supervivencia sin rewards.

## Veredicto final

- Strategy identifiable: PARTIALLY, 82/100.
- Direct copy: NO, 12/100.
- Strategy replication: PREFERRED IN SHADOW, 48/100.
- Robustness: 25/100 por concentración de outliers y ausencia de OOS ejecutable.
- Recommended action: BUILD REPLICATION SHADOW BOT.
"""
    (output_dir / "e46m3_handoff.md").write_text(text, encoding="utf-8")


def build(ledger: Path, output_dir: Path) -> dict[str, Any]:
    summary_path = output_dir / "e46m3_forensics_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    strategy_path = output_dir / "e46m3_strategy_clusters.parquet"
    lifecycle_path = output_dir / "e46m3_lifecycles.parquet"
    trades_path = output_dir / "e46m3_trades.parquet"

    db = duckdb.connect()
    strategy_sql_path = str(strategy_path.resolve()).replace("'", "''")
    lifecycle_sql_path = str(lifecycle_path.resolve()).replace("'", "''")
    trades_sql_path = str(trades_path.resolve()).replace("'", "''")
    db.execute(f"CREATE VIEW strategy AS SELECT * FROM read_parquet('{strategy_sql_path}')")
    db.execute(f"CREATE VIEW lifecycle AS SELECT * FROM read_parquet('{lifecycle_sql_path}')")
    db.execute(f"CREATE VIEW trades AS SELECT * FROM read_parquet('{trades_sql_path}')")

    event_sql = """
        SELECT event_slug,
               any_value(market) AS market,
               any_value(category) AS category,
               count(*) AS conditions,
               sum(trade_count) AS fills,
               sum(trade_volume_usd) AS trade_volume_usd,
               sum(official_total_bought_usd) AS total_bought_usd,
               sum(official_pnl_usd) AS pnl_usd,
               sum(merge_count) AS merge_events,
               sum(merge_notional_usd) AS merge_notional_usd,
               sum(redeem_count) AS redeem_events,
               bool_or(negative_risk) AS negative_risk,
               sum(CASE WHEN paired_yes_no THEN 1 ELSE 0 END) AS paired_conditions
        FROM strategy
        WHERE event_slug <> ''
        GROUP BY event_slug
    """
    events = _dict_rows(db.execute(event_sql))
    top_wins = sorted(events, key=lambda row: _number(row["pnl_usd"]), reverse=True)[:20]
    top_losses = sorted(events, key=lambda row: _number(row["pnl_usd"]))[:20]

    confident = _dict_rows(
        db.execute(
            """
            SELECT count(*) AS closed_winning_positions,
                   count(DISTINCT event_slug) AS events,
                   sum(official_total_bought_usd) AS total_bought_usd,
                   sum(official_realized_pnl_usd) AS realized_pnl_usd,
                   median(official_total_bought_usd) AS median_position_bought_usd,
                   avg(official_total_bought_usd) AS mean_position_bought_usd,
                   quantile_cont(official_total_bought_usd, 0.75) AS p75_position_bought_usd,
                   quantile_cont(official_total_bought_usd, 0.90) AS p90_position_bought_usd,
                   quantile_cont(official_total_bought_usd, 0.99) AS p99_position_bought_usd,
                   max(official_total_bought_usd) AS max_position_bought_usd,
                   median(official_avg_price) AS median_entry_price
            FROM lifecycle
            WHERE official_status='CLOSED'
              AND is_winner_outcome
              AND official_realized_pnl_usd > 0
              AND official_avg_price >= 0.80
            """
        )
    )[0]
    confident_fills = _dict_rows(
        db.execute(
            """
            SELECT count(*) AS buy_fills,
                   sum(t.usdc_size) AS buy_notional_usd,
                   median(t.usdc_size) AS median_fill_usd,
                   avg(t.usdc_size) AS mean_fill_usd,
                   quantile_cont(t.usdc_size, 0.75) AS p75_fill_usd,
                   quantile_cont(t.usdc_size, 0.90) AS p90_fill_usd,
                   quantile_cont(t.usdc_size, 0.99) AS p99_fill_usd,
                   max(t.usdc_size) AS max_fill_usd
            FROM trades t
            JOIN lifecycle l ON t.asset=l.asset
            WHERE t.side='BUY'
              AND t.price >= 0.80
              AND l.official_status='CLOSED'
              AND l.is_winner_outcome
              AND l.official_realized_pnl_usd > 0
            """
        )
    )[0]
    largest_confident_wins = _dict_rows(
        db.execute(
            """
            SELECT market,event_slug,outcome,official_avg_price,official_total_bought_usd,
                   official_realized_pnl_usd,buy_count,buy_vwap,estimated_hold_seconds
            FROM lifecycle
            WHERE official_status='CLOSED'
              AND is_winner_outcome
              AND official_realized_pnl_usd > 0
              AND official_avg_price >= 0.80
            ORDER BY official_total_bought_usd DESC
            LIMIT 20
            """
        )
    )
    confident_all = _dict_rows(
        db.execute(
            """
            SELECT count(*) AS closed_positions,
                   sum(official_realized_pnl_usd > 0) AS profitable_positions,
                   sum(official_realized_pnl_usd < 0) AS losing_positions,
                   sum(official_total_bought_usd) AS total_bought_usd,
                   sum(official_realized_pnl_usd) AS realized_pnl_usd,
                   sum(CASE WHEN official_realized_pnl_usd>0 THEN official_realized_pnl_usd ELSE 0 END) /
                     nullif(-sum(CASE WHEN official_realized_pnl_usd<0 THEN official_realized_pnl_usd ELSE 0 END),0) AS profit_factor,
                   sum(official_realized_pnl_usd > 0)::DOUBLE / count(*) AS profitable_fraction
            FROM lifecycle
            WHERE official_status='CLOSED' AND official_avg_price >= 0.80
            """
        )
    )[0]
    largest_confident_losses = _dict_rows(
        db.execute(
            """
            SELECT market,event_slug,outcome,official_avg_price,official_total_bought_usd,
                   official_realized_pnl_usd,buy_count,buy_vwap,estimated_hold_seconds
            FROM lifecycle
            WHERE official_status='CLOSED'
              AND official_realized_pnl_usd < 0
              AND official_avg_price >= 0.80
            ORDER BY official_realized_pnl_usd ASC
            LIMIT 20
            """
        )
    )
    closed_entry_bands = _dict_rows(
        db.execute(
            """
            SELECT CASE
                     WHEN official_avg_price < .05 THEN '0-5c'
                     WHEN official_avg_price < .10 THEN '5-10c'
                     WHEN official_avg_price < .20 THEN '10-20c'
                     WHEN official_avg_price < .30 THEN '20-30c'
                     WHEN official_avg_price < .40 THEN '30-40c'
                     WHEN official_avg_price < .50 THEN '40-50c'
                     WHEN official_avg_price < .60 THEN '50-60c'
                     WHEN official_avg_price < .70 THEN '60-70c'
                     WHEN official_avg_price < .80 THEN '70-80c'
                     WHEN official_avg_price < .90 THEN '80-90c'
                     WHEN official_avg_price < .95 THEN '90-95c'
                     WHEN official_avg_price < .98 THEN '95-98c'
                     ELSE '98-100c' END AS price_band,
                   count(*) AS positions,
                   sum(official_total_bought_usd) AS total_bought_usd,
                   sum(official_realized_pnl_usd) AS realized_pnl_usd,
                   sum(official_realized_pnl_usd > 0)::DOUBLE / count(*) AS profitable_fraction,
                   sum(CASE WHEN official_realized_pnl_usd>0 THEN official_realized_pnl_usd ELSE 0 END) /
                     nullif(-sum(CASE WHEN official_realized_pnl_usd<0 THEN official_realized_pnl_usd ELSE 0 END),0) AS profit_factor
            FROM lifecycle
            WHERE official_status='CLOSED' AND official_avg_price IS NOT NULL
            GROUP BY price_band
            ORDER BY min(official_avg_price)
            """
        )
    )

    closed_event_rows = _dict_rows(
        db.execute(
            """
            SELECT event_slug,any_value(market) AS market,any_value(category) AS category,
                   sum(official_realized_pnl_usd) AS pnl_usd,
                   sum(official_total_bought_usd) AS total_bought_usd,
                   sum(buy_count + sell_count) AS fills
            FROM lifecycle
            WHERE official_status='CLOSED' AND event_slug<>''
            GROUP BY event_slug
            """
        )
    )
    top_closed_wins = sorted(closed_event_rows, key=lambda row: _number(row["pnl_usd"]), reverse=True)[:20]
    top_closed_losses = sorted(closed_event_rows, key=lambda row: _number(row["pnl_usd"]))[:20]

    buy_price_bands = _dict_rows(
        db.execute(
            """
            SELECT CASE
                     WHEN price < .05 THEN '0-5c'
                     WHEN price < .10 THEN '5-10c'
                     WHEN price < .20 THEN '10-20c'
                     WHEN price < .40 THEN '20-40c'
                     WHEN price < .60 THEN '40-60c'
                     WHEN price < .80 THEN '60-80c'
                     WHEN price < .90 THEN '80-90c'
                     WHEN price < .95 THEN '90-95c'
                     WHEN price < .98 THEN '95-98c'
                     ELSE '98-100c' END AS price_band,
                   count(*) AS fills,
                   sum(usdc_size) AS notional_usd,
                   median(usdc_size) AS median_fill_usd,
                   avg(usdc_size) AS mean_fill_usd,
                   quantile_cont(usdc_size,.90) AS p90_fill_usd
            FROM trades
            WHERE side='BUY'
            GROUP BY price_band
            ORDER BY min(price)
            """
        )
    )
    side_flow = _dict_rows(
        db.execute(
            """
            SELECT side,count(*) AS fills,sum(usdc_size) AS notional_usd,
                   median(usdc_size) AS median_fill_usd,avg(price) AS mean_price
            FROM trades GROUP BY side ORDER BY side
            """
        )
    )

    ledger_db = sqlite3.connect(ledger)
    event_types = _sqlite_rows(
        ledger_db,
        """SELECT event_type,count(*) AS events,sum(usdc_size) AS gross_usdc,
                  sum(cash_delta_usd) AS cash_delta_usd
           FROM activity_events GROUP BY event_type ORDER BY events DESC""",
    )
    tx_mix = _sqlite_rows(
        ledger_db,
        """WITH tx AS (
             SELECT transaction_hash,
                    sum(event_type='TRADE') AS trades,
                    sum(event_type='CONVERSION') AS conversions,
                    sum(event_type='MERGE') AS merges,
                    sum(event_type='SPLIT') AS splits,
                    sum(event_type='REDEEM') AS redeems,
                    count(*) AS events
             FROM activity_events
             WHERE transaction_hash IS NOT NULL AND transaction_hash<>''
             GROUP BY transaction_hash
           )
           SELECT count(*) AS transactions,
                  sum(trades>0) AS with_trade,
                  sum(conversions>0) AS with_conversion,
                  sum(merges>0) AS with_merge,
                  sum(splits>0) AS with_split,
                  sum(redeems>0) AS with_redeem,
                  sum(trades>0 AND conversions>0) AS trade_and_conversion,
                  sum(trades>0 AND merges>0) AS trade_and_merge,
                  sum(conversions>0 AND merges>0) AS conversion_and_merge,
                  max(events) AS max_events_in_transaction,
                  avg(events) AS mean_events_in_transaction
           FROM tx""",
    )[0]
    recent_internal_samples = _sqlite_rows(
        ledger_db,
        """SELECT event_type,timestamp,transaction_hash,condition_id,asset,side,size,price,
                  usdc_size,cash_delta_usd,raw_json
           FROM activity_events
           WHERE event_type IN ('CONVERSION','MERGE','SPLIT')
           ORDER BY timestamp DESC LIMIT 25""",
    )
    for row in recent_internal_samples:
        try:
            raw = json.loads(str(row.pop("raw_json") or "{}"))
        except json.JSONDecodeError:
            raw = {}
        row["event_slug"] = raw.get("eventSlug")
        row["title"] = raw.get("title")
        row["outcome"] = raw.get("outcome")

    last_trade_by_event: dict[str, int] = {}
    last_conversion_by_event: dict[str, int] = {}
    conversion_after_trade_gaps: list[int] = []
    merge_after_conversion_gaps: list[int] = []
    merge_after_trade_gaps: list[int] = []
    seen_conversion_tx: set[str] = set()
    seen_merge_tx: set[str] = set()
    cursor = ledger_db.execute(
        """SELECT event_type,timestamp,transaction_hash,raw_json
           FROM activity_events
           WHERE event_type IN ('TRADE','CONVERSION','MERGE')
           ORDER BY timestamp,event_id"""
    )
    for event_type, timestamp, tx_hash, raw_json in cursor:
        try:
            raw = json.loads(str(raw_json or "{}"))
        except json.JSONDecodeError:
            continue
        event_slug = str(raw.get("eventSlug") or "")
        if not event_slug:
            continue
        timestamp = int(timestamp)
        if event_type == "TRADE":
            last_trade_by_event[event_slug] = timestamp
        elif event_type == "CONVERSION":
            tx_key = str(tx_hash or f"{event_slug}:{timestamp}")
            if tx_key not in seen_conversion_tx:
                seen_conversion_tx.add(tx_key)
                if event_slug in last_trade_by_event:
                    conversion_after_trade_gaps.append(timestamp - last_trade_by_event[event_slug])
                last_conversion_by_event[event_slug] = timestamp
        elif event_type == "MERGE":
            tx_key = str(tx_hash or f"{event_slug}:{timestamp}")
            if tx_key not in seen_merge_tx:
                seen_merge_tx.add(tx_key)
                if event_slug in last_conversion_by_event:
                    merge_after_conversion_gaps.append(timestamp - last_conversion_by_event[event_slug])
                if event_slug in last_trade_by_event:
                    merge_after_trade_gaps.append(timestamp - last_trade_by_event[event_slug])
    cycle_timing = {
        "conversion_transactions": len(seen_conversion_tx),
        "merge_transactions": len(seen_merge_tx),
        "conversion_after_latest_trade": _gap_stats(conversion_after_trade_gaps),
        "merge_after_latest_conversion": _gap_stats(merge_after_conversion_gaps),
        "merge_after_latest_trade": _gap_stats(merge_after_trade_gaps),
        "interpretation": "Event-level nearest-prior-operation gaps; structural timing, not price alpha or causal matching.",
    }
    ledger_db.close()

    rounded_sizes = _dict_rows(
        db.execute(
            """
            SELECT round(size,2) AS shares,count(*) AS fills,
                   sum(usdc_size) AS notional_usd
            FROM trades
            GROUP BY round(size,2)
            ORDER BY fills DESC LIMIT 20
            """
        )
    )
    top_markets_by_volume = sorted(events, key=lambda row: _number(row["trade_volume_usd"]), reverse=True)[:20]
    db.close()

    closed_total = _number(summary["official"]["closed_realized_pnl_usd"])
    without_top5 = _number(summary["risk"]["outlier_sensitivity"]["without_best_5_usd"])
    maker_fraction = (
        _number(summary["onchain_recent"]["maker_fills"]) / _number(summary["onchain_recent"]["fills"])
        if _number(summary["onchain_recent"]["fills"])
        else None
    )
    event_type_counts = {row["event_type"]: int(row["events"]) for row in event_types}
    internal_events = sum(event_type_counts.get(kind, 0) for kind in ("CONVERSION", "MERGE", "SPLIT", "REDEEM"))
    strategy_assessment = {
        "primary": "NegRisk inventory conversion and merge recycling",
        "secondary": "broad multi-outcome/low-price inventory acquisition",
        "tertiary": "directional positions in politics, sports and macro markets",
        "maker_dominant_recent": bool(maker_fraction is not None and maker_fraction >= 0.5),
        "maker_fraction_recent": maker_fraction,
        "internal_token_operations": internal_events,
        "internal_operations_per_fill": internal_events / max(1, int(summary["history"]["fills"])),
        "closed_pnl_survives_without_best_5": without_top5 > 0,
        "closed_pnl_usd": closed_total,
        "structural_evidence": [
            "94,518 NegRisk conversions were observed.",
            "146,642 merges recycled complete token sets into collateral.",
            "The recent on-chain sample is taker-dominant, so market making is not the primary supported label.",
            "The profile exposes repeated equal-sized low-price positions across many outcomes of the same event, consistent with conversion-generated inventory.",
        ],
        "limitations": [
            "Conversion and merge gross amounts are mechanics, not standalone PnL.",
            "No historical executable orderbook exists in the collected dataset.",
            "The leaderboard and position snapshots disagree and must not be added together.",
            "Closed-event PnL becomes negative after removing the five largest winners.",
        ],
    }
    scorecard = {
        "strategy_identifiability": 82,
        "copyability": 12,
        "replicability": 48,
        "capital_efficiency": 38,
        "automation_ease": 90,
        "data_quality": 76,
        "robustness": 25,
        "classification": "B_PROVISIONAL_REPLICATION_BETTER_THAN_COPYING",
        "method": "Heuristic evidence score, not a statistical probability.",
    }
    result = {
        "profile": summary["identity"],
        "history": summary["history"],
        "official": summary["official"],
        "incentives": summary["incentives"],
        "event_types": event_types,
        "transaction_mix": tx_mix,
        "cycle_timing": cycle_timing,
        "side_flow": side_flow,
        "buy_price_bands": buy_price_bands,
        "confident_closed_winners_price_ge_80c": confident,
        "all_closed_positions_price_ge_80c": confident_all,
        "confident_winner_buy_fills_price_ge_80c": confident_fills,
        "largest_confident_closed_wins": largest_confident_wins,
        "largest_confident_closed_losses": largest_confident_losses,
        "closed_entry_price_bands": closed_entry_bands,
        "top_closed_event_wins": top_closed_wins,
        "top_closed_event_losses": top_closed_losses,
        "top_event_wins": top_wins,
        "top_event_losses": top_losses,
        "top_events_by_trade_volume": top_markets_by_volume,
        "rounded_share_sizes": rounded_sizes,
        "recent_internal_operation_samples": recent_internal_samples,
        "strategy_assessment": strategy_assessment,
        "scorecard": scorecard,
    }
    summary["scorecard"] = scorecard
    summary["scores"] = scorecard
    summary["strategy_assessment"] = strategy_assessment
    summary["cycle_timing"] = cycle_timing
    summary["deep_metrics_file"] = "e46m3_deep_metrics.json"
    summary_path.write_text(json.dumps(_clean(summary), indent=2, ensure_ascii=False), encoding="utf-8")
    target = output_dir / "e46m3_deep_metrics.json"
    target.write_text(json.dumps(_clean(result), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_report(result, summary, output_dir)
    _write_handoff(result, summary, output_dir)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Capa cuantitativa profunda específica para @e46m3")
    parser.add_argument("--ledger", default="data/polyledger/e46m3.db")
    parser.add_argument("--output-dir", default="data/e46m3_forensics")
    args = parser.parse_args(argv)
    result = build(Path(args.ledger), Path(args.output_dir))
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "primary": result["strategy_assessment"]["primary"],
                "copyability": result["scorecard"]["copyability"],
                "report_data": str((Path(args.output_dir) / "e46m3_deep_metrics.json").resolve()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

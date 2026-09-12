from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
PREREG_SCHEMA = "prereg_wallet_activity_24h_1"
SNAPSHOT_SCHEMA = "wallet_activity_snapshot_24h_1"
RESULT_SCHEMA = "wallet_activity_analysis_24h_1"
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
BTC_5M_RE = re.compile(r"^btc-updown-5m-(\d{10})$")


class WalletWindowError(RuntimeError):
    """Raised when public wallet evidence is incomplete or inconsistent."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _request_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "polymarker-quantbot-wallet-truth-lab/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise WalletWindowError(f"API pública respondió HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WalletWindowError(f"No se pudo consultar la API pública: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WalletWindowError("La API pública devolvió JSON inválido") from exc


def _url(base: str, path: str, parameters: dict[str, Any]) -> str:
    return f"{base}{path}?{urllib.parse.urlencode(parameters)}"


def resolve_public_identity(
    username: str,
    *,
    fetch_json: Callable[[str], Any] = _request_json,
) -> dict[str, Any]:
    normalized = username.strip()
    if not normalized:
        raise WalletWindowError("Username vacío")
    leaderboard_url = _url(
        DATA_API,
        "/v1/leaderboard",
        {
            "userName": normalized,
            "timePeriod": "ALL",
            "orderBy": "VOL",
            "limit": 50,
        },
    )
    leaderboard = fetch_json(leaderboard_url)
    if not isinstance(leaderboard, list):
        raise WalletWindowError("Leaderboard con estructura inesperada")
    matches = [
        row
        for row in leaderboard
        if isinstance(row, dict)
        and str(row.get("userName") or "").casefold() == normalized.casefold()
    ]
    if len(matches) != 1:
        raise WalletWindowError(
            f"El username debe resolver una identidad única; encontró {len(matches)}"
        )
    leaderboard_row = matches[0]
    wallet = str(leaderboard_row.get("proxyWallet") or "").lower()
    if WALLET_RE.fullmatch(wallet) is None:
        raise WalletWindowError("Leaderboard sin proxyWallet válida")

    profile_url = _url(GAMMA_API, "/public-profile", {"address": wallet})
    profile = fetch_json(profile_url)
    if not isinstance(profile, dict):
        raise WalletWindowError("Perfil público con estructura inesperada")
    if str(profile.get("proxyWallet") or "").lower() != wallet:
        raise WalletWindowError("El perfil no confirma la proxyWallet")
    if str(profile.get("name") or "").casefold() != normalized.casefold():
        raise WalletWindowError("El perfil no confirma el username")

    return {
        "username": str(profile["name"]),
        "proxy_wallet": wallet,
        "pseudonym": profile.get("pseudonym"),
        "x_username": profile.get("xUsername") or leaderboard_row.get("xUsername"),
        "verified_badge": bool(
            profile.get("verifiedBadge", leaderboard_row.get("verifiedBadge", False))
        ),
        "leaderboard_identity": {
            "rank": leaderboard_row.get("rank"),
            "volume_all_time": leaderboard_row.get("vol"),
            "pnl_all_time": leaderboard_row.get("pnl"),
        },
        "source_urls": {
            "leaderboard": leaderboard_url,
            "public_profile": profile_url,
        },
    }


def validate_preregistration(
    payload: dict[str, Any],
    *,
    module_path: str | Path = Path(__file__),
) -> dict[str, Any]:
    if payload.get("schema") != PREREG_SCHEMA:
        raise WalletWindowError("Prerregistro incompatible")
    if payload.get("analysis_module_sha256") != sha256_file(module_path):
        raise WalletWindowError("El módulo de análisis no coincide con el prerregistro")
    username = str(payload.get("username") or "").strip()
    wallet = str(payload.get("proxy_wallet") or "").lower()
    if not username or WALLET_RE.fullmatch(wallet) is None:
        raise WalletWindowError("Identidad congelada inválida")
    start = int(payload.get("window_start_unix", -1))
    end = int(payload.get("window_end_exclusive_unix", -1))
    duration = end - start
    if duration <= 0 or duration > 86_400:
        raise WalletWindowError("La ventana debe ser positiva y de máximo 24 horas")
    if float(payload.get("window_hours", -1)) != duration / 3600.0:
        raise WalletWindowError("window_hours no coincide con los timestamps")
    safety = payload.get("safety")
    expected_safety = {
        "wallet_connection_required": False,
        "orders_enabled": False,
        "real_money": "BLOQUEADO",
        "active_forward_read": False,
        "active_forward_modified": False,
        "market_outcomes_read": False,
        "maximum_window_hours": 24,
    }
    if not isinstance(safety, dict):
        raise WalletWindowError("Bloque safety ausente")
    for key, expected in expected_safety.items():
        if safety.get(key) != expected:
            raise WalletWindowError(f"Safety fail-closed incumplido: {key}")
    return {
        "username": username,
        "proxy_wallet": wallet,
        "start": start,
        "end": end,
        "duration_seconds": duration,
        "safety": expected_safety,
    }


def fetch_activity_window(
    wallet: str,
    start: int,
    end_exclusive: int,
    *,
    fetch_json: Callable[[str], Any] = _request_json,
    segment_seconds: int = 900,
    page_limit: int = 500,
) -> tuple[list[dict[str, Any]], list[dict[str, int]]]:
    wallet = wallet.lower()
    if WALLET_RE.fullmatch(wallet) is None:
        raise WalletWindowError("Wallet inválida")
    duration = int(end_exclusive) - int(start)
    if duration <= 0 or duration > 86_400:
        raise WalletWindowError("Captura rechazada: máximo 24 horas")
    if segment_seconds <= 0 or page_limit <= 0 or page_limit > 500:
        raise WalletWindowError("Configuración de paginación inválida")

    rows: list[dict[str, Any]] = []
    pages: list[dict[str, int]] = []
    segment_start = int(start)
    while segment_start < int(end_exclusive):
        segment_end = min(segment_start + segment_seconds, int(end_exclusive))
        offset = 0
        while True:
            request_url = _url(
                DATA_API,
                "/activity",
                {
                    "user": wallet,
                    "type": "TRADE",
                    "start": segment_start,
                    "end": segment_end - 1,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "ASC",
                    "limit": page_limit,
                    "offset": offset,
                },
            )
            page = fetch_json(request_url)
            if not isinstance(page, list) or any(
                not isinstance(item, dict) for item in page
            ):
                raise WalletWindowError("Página de actividad inválida")
            for item in page:
                timestamp = int(item.get("timestamp", -1))
                if not (segment_start <= timestamp < segment_end):
                    raise WalletWindowError("La API devolvió una fila fuera del segmento")
                if str(item.get("proxyWallet") or "").lower() != wallet:
                    raise WalletWindowError("La API devolvió actividad de otra wallet")
                if str(item.get("type") or "") != "TRADE":
                    raise WalletWindowError("La API devolvió actividad que no es TRADE")
                rows.append(dict(item))
            pages.append(
                {
                    "start": segment_start,
                    "end_inclusive": segment_end - 1,
                    "offset": offset,
                    "count": len(page),
                }
            )
            if len(page) < page_limit:
                break
            offset += page_limit
            if offset > 10_000:
                raise WalletWindowError(
                    "Un segmento excedió la paginación oficial de 10.000 filas"
                )
        segment_start = segment_end

    rows.sort(
        key=lambda item: (
            int(item.get("timestamp", 0)),
            str(item.get("transactionHash") or ""),
            str(item.get("asset") or ""),
            str(item.get("side") or ""),
            str(item.get("outcome") or ""),
            float(item.get("price") or 0),
            float(item.get("size") or 0),
        )
    )
    return rows, pages


def capture_snapshot(
    prereg: dict[str, Any],
    *,
    fetch_json: Callable[[str], Any] = _request_json,
) -> dict[str, Any]:
    frozen = validate_preregistration(prereg)
    identity = resolve_public_identity(frozen["username"], fetch_json=fetch_json)
    if identity["proxy_wallet"] != frozen["proxy_wallet"]:
        raise WalletWindowError("La wallet actual no coincide con la identidad congelada")
    rows, pages = fetch_activity_window(
        frozen["proxy_wallet"],
        frozen["start"],
        frozen["end"],
        fetch_json=fetch_json,
    )
    return {
        "schema": SNAPSHOT_SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "identity": identity,
        "window": {
            "start_unix": frozen["start"],
            "end_exclusive_unix": frozen["end"],
            "start_utc": datetime.fromtimestamp(
                frozen["start"], timezone.utc
            ).isoformat(),
            "end_exclusive_utc": datetime.fromtimestamp(
                frozen["end"], timezone.utc
            ).isoformat(),
            "hours": frozen["duration_seconds"] / 3600.0,
        },
        "query": {
            "endpoint": f"{DATA_API}/activity",
            "type": "TRADE",
            "sort": "TIMESTAMP_ASC",
            "segment_seconds": 900,
            "page_limit": 500,
            "pages": pages,
        },
        "activity_count": len(rows),
        "activities": rows,
        "safety": frozen["safety"],
    }


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise WalletWindowError(f"Valor numérico inválido: {field}") from exc
    if not math.isfinite(result):
        raise WalletWindowError(f"Valor no finito: {field}")
    return result


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def analyze_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise WalletWindowError("Snapshot incompatible")
    window = snapshot.get("window")
    activities = snapshot.get("activities")
    if not isinstance(window, dict) or not isinstance(activities, list):
        raise WalletWindowError("Snapshot incompleto")
    hours = _number(window.get("hours"), "window.hours")
    if hours <= 0 or hours > 24:
        raise WalletWindowError("Snapshot fuera de la política de 24 horas")

    btc_rows = [
        row
        for row in activities
        if isinstance(row, dict) and BTC_5M_RE.fullmatch(str(row.get("slug") or ""))
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in btc_rows:
        condition = str(row.get("conditionId") or "")
        if not condition:
            raise WalletWindowError("Actividad BTC 5m sin conditionId")
        grouped[condition].append(row)

    summaries: list[dict[str, Any]] = []
    total_pair_shares = 0.0
    total_pair_cost = 0.0
    total_buy_cost = 0.0
    total_switches = 0
    for condition, rows in grouped.items():
        outcomes: dict[str, dict[str, float]] = {
            "Up": {"shares": 0.0, "cost": 0.0},
            "Down": {"shares": 0.0, "cost": 0.0},
        }
        buys = []
        sells = 0
        for row in rows:
            side = str(row.get("side") or "").upper()
            if side == "SELL":
                sells += 1
                continue
            if side != "BUY":
                raise WalletWindowError("Side desconocido en actividad")
            outcome_key = str(row.get("outcome") or "").casefold()
            outcome = "Up" if outcome_key == "up" else "Down" if outcome_key == "down" else None
            if outcome is None:
                continue
            size = _number(row.get("size"), "size")
            price = _number(row.get("price"), "price")
            if size < 0 or not (0 <= price <= 1):
                raise WalletWindowError("Ejecución con size o price inválido")
            outcomes[outcome]["shares"] += size
            outcomes[outcome]["cost"] += size * price
            buys.append((int(row["timestamp"]), outcome))

        up = outcomes["Up"]
        down = outcomes["Down"]
        avg_up = up["cost"] / up["shares"] if up["shares"] else None
        avg_down = down["cost"] / down["shares"] if down["shares"] else None
        pair_shares = min(up["shares"], down["shares"])
        complete_set_cost = (
            float(avg_up) + float(avg_down)
            if avg_up is not None and avg_down is not None
            else None
        )
        pair_cost = pair_shares * complete_set_cost if complete_set_cost else 0.0
        gross_buy_cost = up["cost"] + down["cost"]
        ordered_outcomes = [outcome for _, outcome in sorted(buys)]
        switches = sum(
            1
            for previous, current in zip(ordered_outcomes, ordered_outcomes[1:])
            if previous != current
        )
        total_switches += switches
        total_pair_shares += pair_shares
        total_pair_cost += pair_cost
        total_buy_cost += gross_buy_cost
        slug = str(rows[0].get("slug") or "")
        slug_match = BTC_5M_RE.fullmatch(slug)
        market_start = int(slug_match.group(1)) if slug_match else None
        summaries.append(
            {
                "condition_id": condition,
                "slug": slug,
                "market_start_unix": market_start,
                "activity_records": len(rows),
                "buy_records": len(buys),
                "sell_records": sells,
                "up_bought_shares": up["shares"],
                "down_bought_shares": down["shares"],
                "average_up_buy_price": avg_up,
                "average_down_buy_price": avg_down,
                "pairable_gross_buy_shares": pair_shares,
                "estimated_complete_set_cost": complete_set_cost,
                "estimated_paired_cost": pair_cost,
                "gross_buy_cost": gross_buy_cost,
                "estimated_paired_capital_fraction": (
                    pair_cost / gross_buy_cost if gross_buy_cost else None
                ),
                "directional_residual_side": (
                    "Up" if up["shares"] > down["shares"] else "Down"
                    if down["shares"] > up["shares"]
                    else None
                ),
                "directional_residual_shares": abs(
                    up["shares"] - down["shares"]
                ),
                "buy_outcome_switches": switches,
            }
        )
    summaries.sort(key=lambda row: (row["market_start_unix"] or 0, row["condition_id"]))

    transaction_hashes = {
        str(row.get("transactionHash"))
        for row in btc_rows
        if row.get("transactionHash")
    }
    active_hours = {int(row["timestamp"]) // 3600 for row in btc_rows}
    record_values = [
        _number(row.get("usdcSize"), "usdcSize")
        for row in btc_rows
        if row.get("usdcSize") is not None
    ]
    set_costs = [
        float(row["estimated_complete_set_cost"])
        for row in summaries
        if row["estimated_complete_set_cost"] is not None
    ]
    paired_markets = len(set_costs)
    paired_fraction = total_pair_cost / total_buy_cost if total_buy_cost else None
    weighted_set_cost = total_pair_cost / total_pair_shares if total_pair_shares else None
    unique_tx_active_hour = (
        len(transaction_hashes) / len(active_hours) if active_hours else 0.0
    )
    mean_record_value = sum(record_values) / len(record_values) if record_values else None

    return {
        "schema": RESULT_SCHEMA,
        "created_from_snapshot_at": snapshot.get("captured_at"),
        "identity": snapshot.get("identity"),
        "window": dict(window),
        "scope": "Actividad pública ejecutada; no reconstruye órdenes no llenadas ni prioridad de cola.",
        "verdict": "DESCRIPTIVE_ONLY",
        "metrics": {
            "all_public_activity_records": len(activities),
            "btc_5m_activity_records": len(btc_rows),
            "btc_5m_records_per_window_hour": len(btc_rows) / hours,
            "unique_transaction_hashes": len(transaction_hashes),
            "unique_transactions_per_active_hour": unique_tx_active_hour,
            "active_utc_hours": len(active_hours),
            "btc_5m_markets": len(summaries),
            "markets_with_both_sides_bought": paired_markets,
            "markets_with_both_sides_bought_fraction": (
                paired_markets / len(summaries) if summaries else None
            ),
            "estimated_weighted_complete_set_cost": weighted_set_cost,
            "estimated_paired_capital_fraction": paired_fraction,
            "estimated_directional_residual_capital_fraction": (
                1.0 - paired_fraction if paired_fraction is not None else None
            ),
            "mean_public_activity_record_usdc": mean_record_value,
            "buy_outcome_switches": total_switches,
            "complete_set_cost_market_quantiles": {
                "p10": _quantile(set_costs, 0.10),
                "p50": _quantile(set_costs, 0.50),
                "p90": _quantile(set_costs, 0.90),
            },
        },
        "claim_comparison": [
            {
                "claim": "average complete-set cost",
                "claimed_value": 0.9843,
                "observed_proxy": weighted_set_cost,
                "status": "DESCRIPTIVE_PROXY_NOT_ORDER_RECONSTRUCTION",
            },
            {
                "claim": "paired capital fraction",
                "claimed_value": 0.787,
                "observed_proxy": paired_fraction,
                "status": "DESCRIPTIVE_PROXY_NOT_POSITION_ACCOUNTING",
            },
            {
                "claim": "trades per active hour",
                "claimed_value": 51.25,
                "observed_proxy": unique_tx_active_hour,
                "status": "UNIT_NOT_CONFIRMED_BY_SOURCE_POST",
            },
            {
                "claim": "average trade USD",
                "claimed_value": 110.67,
                "observed_proxy": mean_record_value,
                "status": "UNIT_NOT_CONFIRMED_BY_SOURCE_POST",
            },
        ],
        "limitations": [
            "La API pública expone actividad ejecutada, no órdenes abiertas, canceladas o rechazadas.",
            "No identifica de forma suficiente maker/taker ni prioridad de cola para cada registro.",
            "Una transacción puede agrupar varios registros; 'trade' no tiene una unidad pública inequívoca.",
            "El coste del set usa promedios ponderados de compras UP y DOWN; no emparejamiento exacto de lotes.",
            "No se leyeron resoluciones, labels ni outcomes ganadores y no se calculó PnL.",
        ],
        "market_summaries": summaries,
        "safety": snapshot.get("safety"),
    }


def encoded_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def write_json_exclusive_or_verify(
    path: str | Path, payload: dict[str, Any]
) -> str:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = encoded_json(payload)
    if target.exists():
        if target.read_bytes() != expected:
            raise WalletWindowError(f"El archivo existente no coincide: {target}")
        return "VERIFIED_EXISTING"
    partial = target.with_suffix(target.suffix + ".partial")
    if partial.exists():
        raise WalletWindowError(f"Existe un archivo parcial: {partial}")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    partial.replace(target)
    return "CREATED"


__all__ = [
    "PREREG_SCHEMA",
    "RESULT_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "WalletWindowError",
    "analyze_snapshot",
    "capture_snapshot",
    "fetch_activity_window",
    "resolve_public_identity",
    "sha256_file",
    "validate_preregistration",
    "write_json_exclusive_or_verify",
]

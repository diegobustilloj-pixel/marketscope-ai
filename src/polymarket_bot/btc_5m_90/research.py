from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contract import FIRST_TOUCH_SEMANTICS, validate_gamma_market


ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = ROOT / "data" / "btc5m90_v001"
PROTOCOL_PATH = OUTPUT_DIR / "protocol.json"
LABELS_PATH = OUTPUT_DIR / "raw" / "gamma_markets.json"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
PROTOCOL_SCHEMA = "btc5m90_protocol_v001"
LABEL_SCHEMA = "btc5m90_gamma_raw_v001"


class ResearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSpec:
    name: str
    database: str
    market_table: str
    sample_table: str
    split: str
    sampling_ms: int
    execution_quality: str
    ask_depth_field: str | None = None


SOURCES = (
    SourceSpec(
        "v018",
        "data/paper_v018_multifill.db",
        "v018_markets",
        "v018_seconds",
        "TRAIN",
        1000,
        "LEVEL_B_ASK_TRAJECTORY_NO_EXACT_LEVEL_SIZE",
    ),
    SourceSpec(
        "v019",
        "data/paper_v019_fifo_pair.db",
        "v019_markets",
        "v019_seconds",
        "TRAIN",
        1000,
        "LEVEL_B_ASK_TRAJECTORY_NO_EXACT_LEVEL_SIZE",
    ),
    SourceSpec(
        "v020",
        "data/paper_v020_mandatory_hedge.db",
        "v020_markets",
        "v020_seconds",
        "VALIDATION",
        1000,
        "LEVEL_B_PLUS_ASK_AND_ONE_CENT_BAND_DEPTH",
        "ask_depth_1c",
    ),
    SourceSpec(
        "v021",
        "data/paper_v021_safe_pair_observer.db",
        "v021_markets",
        "v021_samples",
        "TEST",
        250,
        "LEVEL_B_PLUS_ASK_AND_TOTAL_SIDE_DEPTH",
        "total_ask_depth",
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_json(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_json_once(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _connect_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def load_universe() -> list[dict[str, Any]]:
    universe: list[dict[str, Any]] = []
    previous_start = -1
    for source in SOURCES:
        path = ROOT / source.database
        if not path.is_file():
            raise ResearchError(f"Falta fuente: {path}")
        with _connect_ro(path) as connection:
            if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
                raise ResearchError(f"SQLite quick_check falló: {path}")
            rows = connection.execute(
                f"SELECT condition_id,slug,market_start_ms,market_end_ms,status,"
                f"real_money,orders_sent FROM {source.market_table} "
                "ORDER BY market_start_ms,condition_id"
            ).fetchall()
        for row in rows:
            if str(row["status"]) != "COMPLETE":
                raise ResearchError(f"Mercado no COMPLETE en {source.name}: {row['slug']}")
            if int(row["real_money"]) != 0 or int(row["orders_sent"]) != 0:
                raise ResearchError(f"Fuente no paper-only en {source.name}")
            start_ms = int(row["market_start_ms"])
            if start_ms <= previous_start:
                raise ResearchError("Universo duplicado o fuera de orden cronológico")
            previous_start = start_ms
            universe.append(
                {
                    "source": source.name,
                    "split": source.split,
                    "condition_id": str(row["condition_id"]),
                    "slug": str(row["slug"]),
                    "market_start_ms": start_ms,
                    "market_end_ms": int(row["market_end_ms"]),
                }
            )
    return universe


def _code_paths() -> tuple[Path, ...]:
    return (
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "contract.py",
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "research.py",
        ROOT / "src" / "polymarket_bot" / "btc_5m_90" / "backtest.py",
        ROOT / "btc5m90_v001.py",
    )


def build_protocol() -> dict[str, Any]:
    universe = load_universe()
    source_rows: list[dict[str, Any]] = []
    for source in SOURCES:
        path = ROOT / source.database
        item = asdict(source)
        item["database_sha256"] = sha256_file(path)
        item["markets"] = sum(row["source"] == source.name for row in universe)
        source_rows.append(item)
    code_hashes: dict[str, str] = {}
    for path in _code_paths():
        if not path.is_file():
            raise ResearchError(f"Falta implementación antes del freeze: {path}")
        code_hashes[str(path.relative_to(ROOT)).replace("\\", "/")] = sha256_file(path)
    counts = {
        split: sum(row["split"] == split for row in universe)
        for split in ("TRAIN", "VALIDATION", "TEST")
    }
    return {
        "schema": PROTOCOL_SCHEMA,
        "version": "BTC5M90_V001",
        "status": "FROZEN_BEFORE_VALIDATION_AND_TEST_OUTCOMES",
        "frozen_at": utc_now(),
        "question": "P(win | first executable exact threshold touch) after actual taker fee",
        "interpretation": {
            "primary": FIRST_TOUCH_SEMANTICS,
            "literal_control": (
                "first snapshot ask <= threshold; expected to be ambiguous at market open "
                "and never used as an economic strategy"
            ),
            "entry_price": 0.90,
            "maximum_entry_price": 0.90,
            "one_trade_per_condition_id": True,
            "hold_to_resolution": True,
            "jump_below_to_above": "MISSED_SIGNAL",
            "tie_same_timestamp": "AMBIGUOUS_EXCLUDED",
        },
        "universe": {
            "market_family": "btc-updown-5m",
            "markets": len(universe),
            "period_start_ms": universe[0]["market_start_ms"],
            "period_end_ms": universe[-1]["market_end_ms"],
            "chronological_splits": counts,
            "split_rationale": (
                "V018/V019 labels were already observed and are development only; "
                "V020 is validation and V021 remains final untouched test"
            ),
            "sources": source_rows,
        },
        "analysis": {
            "base_threshold": 0.90,
            "threshold_sensitivity": [round(value / 100, 2) for value in range(85, 96)],
            "latency_ms": [0, 100, 250, 500, 1000, 2000, 5000],
            "unsupported_latency": "N/A; never interpolated",
            "stake_shares": 5.0,
            "time_filter_selection": (
                "candidate contiguous elapsed-time buckets selected on TRAIN only; "
                "must have >=50 TRAIN signals and positive fee-adjusted EV; "
                "one candidate is frozen before VALIDATION and TEST reporting"
            ),
            "no_model": True,
            "no_external_btc_filter": True,
        },
        "promotion_gates": {
            "minimum_test_trades": 150,
            "test_net_ev_per_trade_gt": 0.0,
            "test_one_tick_stress_ev_gt": 0.0,
            "wilson_lower_gt_break_even": True,
            "shadow_hours_max": 24,
            "minimum_shadow_fills": 150,
            "minimum_shadow_execution_rate": 0.80,
            "shadow_net_ev_gt": 0.0,
            "real_money": "BLOQUEADO",
            "automatic_live_promotion": False,
        },
        "execution_limits": {
            "v018_v019": "ask trajectory only; exact size at threshold unavailable",
            "v020": "one-cent ask-depth band; cannot prove exact threshold-level size",
            "v021": "full-side aggregate depth; cannot prove exact threshold-level size",
            "historical_fill_claim": "THEORETICAL/LEVEL_B only",
            "level_a_required": "forward shadow storing every price level and receive timestamp",
        },
        "safety": {
            "mode": "RESEARCH_PAPER_SHADOW_ONLY",
            "wallet_required": False,
            "orders_enabled": False,
            "real_money": "BLOQUEADO",
            "previous_branches_modified": False,
        },
        "code_sha256": code_hashes,
    }


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict[str, Any]:
    payload = build_protocol()
    _write_json_once(path, payload)
    return payload


def load_and_verify_protocol(path: Path = PROTOCOL_PATH) -> dict[str, Any]:
    if not path.is_file():
        raise ResearchError("Primero debe congelarse el protocolo")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != PROTOCOL_SCHEMA:
        raise ResearchError("Protocolo incompatible")
    for relative, expected_hash in payload.get("code_sha256", {}).items():
        if sha256_file(ROOT / relative) != expected_hash:
            raise ResearchError(f"Código cambió después del freeze: {relative}")
    frozen_sources = {
        row["name"]: row for row in payload["universe"]["sources"]
    }
    for source in SOURCES:
        if sha256_file(ROOT / source.database) != frozen_sources[source.name]["database_sha256"]:
            raise ResearchError(f"Fuente cambió después del freeze: {source.name}")
    return payload


def _load_old_cache(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    markets = payload.get("markets")
    if not isinstance(markets, dict):
        raise ResearchError(f"Caché anterior incompatible: {path}")
    return markets


def _load_branch_cache(path: Path = LABELS_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": LABEL_SCHEMA, "created_at": utc_now(), "markets": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != LABEL_SCHEMA or not isinstance(payload.get("markets"), dict):
        raise ResearchError("Caché BTC5M90 incompatible")
    return payload


def _fetch_gamma(slug: str, *, timeout: float = 20.0) -> str:
    url = f"{GAMMA_BASE_URL}/markets/slug/{urllib.parse.quote(slug, safe='')}"
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "PolyMarker-BTC5M90/0.1"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2**attempt)
    raise ResearchError(f"Gamma falló para {slug}: {last_error}")


def fetch_and_freeze_labels(path: Path = LABELS_PATH) -> dict[str, Any]:
    protocol = load_and_verify_protocol()
    universe = load_universe()
    cache = _load_branch_cache(path)
    markets = cache["markets"]

    old_by_source = {
        "v018": _load_old_cache(ROOT / "data" / "v018_resolution_cache.json"),
        "v019": _load_old_cache(ROOT / "data" / "v019_resolution_cache.json"),
    }
    new_since_save = 0
    for index, row in enumerate(universe, start=1):
        slug = row["slug"]
        existing = markets.get(slug)
        if isinstance(existing, dict) and isinstance(existing.get("payload_raw"), str):
            raw = existing["payload_raw"]
        elif row["source"] in old_by_source:
            raw_value = old_by_source[row["source"]].get(slug)
            if not isinstance(raw_value, str):
                raise ResearchError(f"Falta etiqueta histórica esperada: {slug}")
            raw = raw_value
            markets[slug] = {
                "source_url": f"{GAMMA_BASE_URL}/markets/slug/{slug}",
                "retrieved_at": utc_now(),
                "provenance": f"read_only_import_{row['source']}_resolution_cache",
                "payload_raw": raw,
            }
            new_since_save += 1
        else:
            raw = _fetch_gamma(slug)
            markets[slug] = {
                "source_url": f"{GAMMA_BASE_URL}/markets/slug/{slug}",
                "retrieved_at": utc_now(),
                "provenance": "official_gamma_api_direct",
                "payload_raw": raw,
            }
            new_since_save += 1
            time.sleep(0.05)

        try:
            payload = json.loads(raw)
            validate_gamma_market(
                payload,
                expected_slug=slug,
                expected_condition_id=row["condition_id"],
                require_resolution=True,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise ResearchError(f"Contrato/label inválido en {slug}: {exc}") from exc

        if new_since_save >= 10:
            cache["updated_at"] = utc_now()
            cache["protocol_sha256"] = sha256_file(PROTOCOL_PATH)
            _write_json_atomic(path, cache)
            new_since_save = 0
        if index == 1 or index % 50 == 0 or index == len(universe):
            print(f"Gamma BTC5M90: {index}/{len(universe)} verificados")

    cache["updated_at"] = utc_now()
    cache["protocol_sha256"] = sha256_file(PROTOCOL_PATH)
    cache["market_count"] = len(markets)
    cache["content_sha256"] = sha256_json(markets)
    cache["protocol_status"] = protocol["status"]
    _write_json_atomic(path, cache)
    return cache


def load_verified_contracts(path: Path = LABELS_PATH) -> dict[str, Any]:
    load_and_verify_protocol()
    cache = _load_branch_cache(path)
    if cache.get("protocol_sha256") != sha256_file(PROTOCOL_PATH):
        raise ResearchError("Etiquetas no pertenecen a este protocolo")
    universe = load_universe()
    result: dict[str, Any] = {}
    for row in universe:
        item = cache["markets"].get(row["slug"])
        if not isinstance(item, dict) or not isinstance(item.get("payload_raw"), str):
            raise ResearchError(f"Etiqueta ausente: {row['slug']}")
        payload = json.loads(item["payload_raw"])
        result[row["condition_id"]] = validate_gamma_market(
            payload,
            expected_slug=row["slug"],
            expected_condition_id=row["condition_id"],
            require_resolution=True,
        )
    return result


def iter_source_specs(names: Iterable[str] | None = None) -> Iterable[SourceSpec]:
    selected = set(names) if names is not None else None
    return (source for source in SOURCES if selected is None or source.name in selected)

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover
    import fcntl

from .climate_modeling import (
    MIN_LOCAL_CALIBRATION,
    _base_predictions,
    _bucket_probabilities,
    _calibrate,
    _forecast_points,
    _load_inputs,
    _train_weights,
)
from .climate_research import OPEN_METEO_MODELS, iso, parse_dt, stable_json, utc_now


SCHEMA_VERSION = "climate_shadow_forward_v001"
CODE_VERSION = "0.0.1"
DEFAULT_DB = Path("data/climate_shadow_forward_v001.db")
DEFAULT_RESEARCH_ROOT = Path("data/climate_research_v001")
DEFAULT_POLL_SECONDS = 600.0
DEFAULT_FORECAST_REFRESH_SECONDS = 3600.0
THRESHOLDS = (0.03, 0.05, 0.075, 0.10, 0.15)
REQUESTED_NOTIONAL = 100.0
USER_AGENT = "ProyectoBotV4-ClimateShadow/0.0.1"
CLOB = "https://clob.polymarket.com"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
AVIATION_WEATHER = "https://aviationweather.gov/api/data/metar"


class ClimateShadowError(RuntimeError):
    pass


class MonitorLock:
    def __init__(self, database: Path) -> None:
        self.path = database.resolve().with_suffix(database.suffix + ".lock")
        self.handle: Any | None = None

    def __enter__(self) -> "MonitorLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise ClimateShadowError(f"Ya existe un monitor para {self.path}") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS shadow_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_runs(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  requested_end_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL,
  poll_seconds REAL NOT NULL,
  forecast_refresh_seconds REAL NOT NULL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS shadow_cycles(
  cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  duration_seconds REAL,
  active_markets INTEGER DEFAULT 0,
  forecast_rows INTEGER DEFAULT 0,
  observation_rows INTEGER DEFAULT 0,
  quote_rows INTEGER DEFAULT 0,
  decision_rows INTEGER DEFAULT 0,
  edge_candidates INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0,
  error TEXT,
  FOREIGN KEY(run_id) REFERENCES shadow_runs(run_id)
);

CREATE TABLE IF NOT EXISTS shadow_markets(
  slug TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  city TEXT NOT NULL,
  market_type TEXT NOT NULL,
  market_date TEXT NOT NULL,
  station_id TEXT NOT NULL,
  unit TEXT NOT NULL,
  precision REAL NOT NULL,
  strict_contract INTEGER NOT NULL,
  contract_reason TEXT NOT NULL,
  forecastability_score REAL,
  sample_grade TEXT,
  fee_schedule_json TEXT,
  source_type TEXT,
  resolution_source TEXT,
  description_sha256 TEXT,
  buckets_json TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_forecasts(
  forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  station_id TEXT NOT NULL,
  market_date TEXT NOT NULL,
  model TEXT NOT NULL,
  market_type TEXT NOT NULL,
  lead_days INTEGER NOT NULL,
  timezone TEXT NOT NULL,
  raw_point_c REAL,
  raw_point_native REAL,
  calibrated_point_native REAL,
  calibration_scope TEXT,
  bias_native REAL,
  sigma_native REAL,
  payload_hash TEXT NOT NULL,
  FOREIGN KEY(cycle_id) REFERENCES shadow_cycles(cycle_id)
);

CREATE TABLE IF NOT EXISTS shadow_probabilities(
  probability_id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL,
  slug TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  candidate TEXT NOT NULL,
  lead_days INTEGER NOT NULL,
  point_native REAL NOT NULL,
  model_count INTEGER NOT NULL,
  top_bucket TEXT NOT NULL,
  top_probability REAL NOT NULL,
  probabilities_json TEXT NOT NULL,
  FOREIGN KEY(cycle_id) REFERENCES shadow_cycles(cycle_id),
  FOREIGN KEY(slug) REFERENCES shadow_markets(slug)
);

CREATE TABLE IF NOT EXISTS shadow_observations(
  station_id TEXT NOT NULL,
  observation_time TEXT NOT NULL,
  report_time TEXT,
  receipt_time TEXT,
  temperature_c REAL,
  dewpoint_c REAL,
  wind_direction REAL,
  wind_speed_knots REAL,
  visibility_miles REAL,
  altimeter_hpa REAL,
  weather TEXT,
  cover TEXT,
  raw_observation TEXT,
  raw_hash TEXT NOT NULL,
  first_observed_at TEXT NOT NULL,
  PRIMARY KEY(station_id,observation_time)
);

CREATE TABLE IF NOT EXISTS shadow_station_extremes(
  extreme_id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL,
  station_id TEXT NOT NULL,
  local_date TEXT NOT NULL,
  timezone TEXT NOT NULL,
  maximum_c REAL,
  minimum_c REAL,
  observations INTEGER NOT NULL,
  latest_observation_time TEXT,
  age_seconds REAL,
  FOREIGN KEY(cycle_id) REFERENCES shadow_cycles(cycle_id)
);

CREATE TABLE IF NOT EXISTS shadow_quotes(
  quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL,
  slug TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  bucket TEXT NOT NULL,
  side TEXT NOT NULL,
  token TEXT NOT NULL,
  best_bid REAL,
  best_ask REAL,
  bid_size REAL NOT NULL,
  ask_size REAL NOT NULL,
  spread REAL,
  ask_depth_1c_usdc REAL NOT NULL,
  ask_depth_5c_usdc REAL NOT NULL,
  vwap_100_usdc REAL,
  executable_cost_100_usdc REAL NOT NULL,
  book_timestamp TEXT,
  book_hash TEXT,
  FOREIGN KEY(cycle_id) REFERENCES shadow_cycles(cycle_id),
  FOREIGN KEY(slug) REFERENCES shadow_markets(slug)
);

CREATE TABLE IF NOT EXISTS shadow_decisions(
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  probability_id INTEGER NOT NULL,
  slug TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  threshold REAL NOT NULL,
  bucket TEXT,
  side TEXT,
  model_probability REAL,
  executable_ask REAL,
  visible_ask_shares REAL,
  gross_edge REAL,
  expected_roi_gross REAL,
  fee_per_share REAL,
  net_edge REAL,
  expected_roi_net REAL,
  would_pass_edge INTEGER NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  FOREIGN KEY(probability_id) REFERENCES shadow_probabilities(probability_id),
  FOREIGN KEY(slug) REFERENCES shadow_markets(slug)
);

CREATE TABLE IF NOT EXISTS shadow_health(
  health_id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL,
  component TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT,
  observed_at TEXT NOT NULL,
  FOREIGN KEY(cycle_id) REFERENCES shadow_cycles(cycle_id)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_cycle ON shadow_forecasts(cycle_id);
CREATE INDEX IF NOT EXISTS idx_probabilities_cycle ON shadow_probabilities(cycle_id);
CREATE INDEX IF NOT EXISTS idx_quotes_cycle_slug ON shadow_quotes(cycle_id,slug);
CREATE INDEX IF NOT EXISTS idx_decisions_cycle ON shadow_decisions(probability_id);
"""


def http_json(
    url: str,
    *,
    method: str = "GET",
    body: Any | None = None,
    attempts: int = 4,
    timeout: int = 90,
) -> Any:
    encoded = None if body is None else stable_json(body).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8-sig"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(8.0, 0.75 * 2**attempt))
    raise ClimateShadowError(f"No se pudo consultar {url}: {last}")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _float(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _hash_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _native(value_c: float, unit: str) -> float:
    return value_c * 9.0 / 5.0 + 32.0 if unit == "°F" else value_c


def strict_contract(row: dict[str, str]) -> tuple[bool, str]:
    missing: list[str] = []
    if row.get("rules_complete") != "True":
        missing.append("CORE_RULES")
    if row.get("station_metadata_complete") != "True":
        missing.append("STATION_METADATA")
    if not str(row.get("timezone") or "").strip():
        missing.append("TIMEZONE_EXPLICIT")
    frequency = str(row.get("frequency") or "").strip()
    if not frequency or frequency == "OBSERVATION_TABLE_UNRESOLVED":
        missing.append("OBSERVATION_FREQUENCY")
    return not missing, "OK" if not missing else ",".join(missing)


def fee_per_share(price: float, fee_schedule: Any) -> float | None:
    if not isinstance(fee_schedule, dict):
        return None
    try:
        rate = float(fee_schedule["rate"])
        exponent = float(fee_schedule.get("exponent", 1.0))
    except (KeyError, TypeError, ValueError):
        return None
    if rate < 0 or exponent <= 0 or not bool(fee_schedule.get("takerOnly", False)):
        return None
    return rate * ((price * (1.0 - price)) ** exponent)


def book_metrics(book: dict[str, Any] | None, requested_notional: float = REQUESTED_NOTIONAL) -> dict[str, Any]:
    if not book:
        return {
            "best_bid": None,
            "best_ask": None,
            "bid_size": 0.0,
            "ask_size": 0.0,
            "spread": None,
            "ask_depth_1c_usdc": 0.0,
            "ask_depth_5c_usdc": 0.0,
            "vwap_100_usdc": None,
            "executable_cost_100_usdc": 0.0,
        }
    bids = sorted(
        ((float(row["price"]), float(row.get("size") or 0.0)) for row in book.get("bids", []) if row.get("price") is not None),
        reverse=True,
    )
    asks = sorted(
        (float(row["price"]), float(row.get("size") or 0.0)) for row in book.get("asks", []) if row.get("price") is not None
    )
    best_bid, bid_size = bids[0] if bids else (None, 0.0)
    best_ask, ask_size = asks[0] if asks else (None, 0.0)
    depth_1c = sum(price * size for price, size in asks if best_ask is not None and price <= best_ask + 0.0100001)
    depth_5c = sum(price * size for price, size in asks if best_ask is not None and price <= best_ask + 0.0500001)
    remaining = requested_notional
    cost = 0.0
    shares = 0.0
    for price, size in asks:
        take_cost = min(remaining, price * size)
        if price <= 0:
            continue
        shares += take_cost / price
        cost += take_cost
        remaining -= take_cost
        if remaining <= 1e-9:
            break
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread": best_ask - best_bid if best_ask is not None and best_bid is not None else None,
        "ask_depth_1c_usdc": depth_1c,
        "ask_depth_5c_usdc": depth_5c,
        "vwap_100_usdc": cost / shares if shares > 0 else None,
        "executable_cost_100_usdc": cost,
    }


def best_decision(
    probabilities: dict[str, float],
    quotes: dict[tuple[str, str], dict[str, Any]],
    *,
    threshold: float,
    strict: bool,
    contract_reason: str,
    lead_days: int,
    fee_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for bucket, yes_probability in probabilities.items():
        for side, probability in (("YES", yes_probability), ("NO", 1.0 - yes_probability)):
            quote = quotes.get((bucket, side), {})
            ask = quote.get("best_ask")
            if ask is None:
                continue
            price = float(ask)
            gross_edge = probability - price
            fee = fee_per_share(price, fee_schedule)
            net_edge = None if fee is None else gross_edge - fee
            candidates.append(
                {
                    "bucket": bucket,
                    "side": side,
                    "model_probability": probability,
                    "executable_ask": ask,
                    "visible_ask_shares": quote.get("ask_size", 0.0),
                    "gross_edge": gross_edge,
                    "expected_roi_gross": gross_edge / price if price > 0 else None,
                    "fee_per_share": fee,
                    "net_edge": net_edge,
                    "expected_roi_net": net_edge / (price + fee) if net_edge is not None and price + fee > 0 else None,
                }
            )
    if not candidates:
        return {
            "threshold": threshold,
            "bucket": None,
            "side": None,
            "model_probability": None,
            "executable_ask": None,
            "visible_ask_shares": None,
            "gross_edge": None,
            "expected_roi_gross": None,
            "fee_per_share": None,
            "net_edge": None,
            "expected_roi_net": None,
            "would_pass_gross_edge": 0,
            "would_pass_edge": 0,
            "decision": "NO_BOOK",
            "reason": "NO_EXECUTABLE_ASK",
        }
    priced = [row for row in candidates if row["net_edge"] is not None]
    best = max(priced or candidates, key=lambda row: row["net_edge"] if row["net_edge"] is not None else row["gross_edge"])
    passes_gross = best["gross_edge"] >= threshold and best["visible_ask_shares"] > 0
    passes = best["net_edge"] is not None and best["net_edge"] >= threshold and best["visible_ask_shares"] > 0
    if lead_days not in (1, 2, 3, 4):
        decision, reason = "BLOCKED_LEAD", f"D+{lead_days}_NOT_SELECTED"
    elif not strict:
        decision, reason = "BLOCKED_CONTRACT", contract_reason
    elif best["fee_per_share"] is None:
        decision, reason = "BLOCKED_FEE", "FEE_SCHEDULE_UNVERIFIED"
    elif passes:
        decision, reason = "SHADOW_CANDIDATE", "NET_EDGE_AFTER_TAKER_FEE"
    else:
        decision, reason = "WAIT", "EDGE_BELOW_THRESHOLD"
    return {
        "threshold": threshold,
        **best,
        "would_pass_gross_edge": int(passes_gross),
        "would_pass_edge": int(passes),
        "decision": decision,
        "reason": reason,
    }


class ClimateStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60.0)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
        self._migrate()

    def _migrate(self) -> None:
        decision_columns = {row[1] for row in self.db.execute("PRAGMA table_info(shadow_decisions)")}
        decision_additions = {
            "fee_per_share": "REAL",
            "net_edge": "REAL",
            "expected_roi_net": "REAL",
        }
        for name, sql_type in decision_additions.items():
            if name not in decision_columns:
                self.db.execute(f"ALTER TABLE shadow_decisions ADD COLUMN {name} {sql_type}")
        market_columns = {row[1] for row in self.db.execute("PRAGMA table_info(shadow_markets)")}
        market_additions = {
            "fee_schedule_json": "TEXT",
            "source_type": "TEXT",
            "resolution_source": "TEXT",
            "description_sha256": "TEXT",
        }
        for name, sql_type in market_additions.items():
            if name not in market_columns:
                self.db.execute(f"ALTER TABLE shadow_markets ADD COLUMN {name} {sql_type}")
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO shadow_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, stable_json(value)),
        )

    def start_run(self, end_at: datetime, poll_seconds: float, forecast_refresh_seconds: float) -> int:
        cursor = self.db.execute(
            "INSERT INTO shadow_runs(started_at,requested_end_at,status,poll_seconds,forecast_refresh_seconds) VALUES(?,?,?,?,?)",
            (iso(utc_now()), iso(end_at), "RUNNING", poll_seconds, forecast_refresh_seconds),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE shadow_runs SET ended_at=?,status=?,error=? WHERE run_id=?",
            (iso(utc_now()), status, error, run_id),
        )
        self.db.commit()

    def start_cycle(self, run_id: int) -> int:
        cursor = self.db.execute(
            "INSERT INTO shadow_cycles(run_id,started_at,status) VALUES(?,?,?)", (run_id, iso(utc_now()), "RUNNING")
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_cycle(self, cycle_id: int, started: float, status: str, stats: dict[str, int], error: str | None = None) -> None:
        self.db.execute(
            """
            UPDATE shadow_cycles SET finished_at=?,status=?,duration_seconds=?,active_markets=?,forecast_rows=?,
              observation_rows=?,quote_rows=?,decision_rows=?,edge_candidates=?,errors=?,error=? WHERE cycle_id=?
            """,
            (
                iso(utc_now()), status, time.monotonic() - started, stats["active_markets"], stats["forecast_rows"],
                stats["observation_rows"], stats["quote_rows"], stats["decision_rows"], stats["edge_candidates"],
                stats["errors"], error, cycle_id,
            ),
        )
        self.db.commit()

    def health(self, cycle_id: int, component: str, status: str, detail: Any) -> None:
        self.db.execute(
            "INSERT INTO shadow_health(cycle_id,component,status,detail,observed_at) VALUES(?,?,?,?,?)",
            (cycle_id, component, status, stable_json(detail) if not isinstance(detail, str) else detail, iso(utc_now())),
        )


class ClimateShadowMonitor:
    def __init__(self, database: Path, research_root: Path, *, workers: int = 8) -> None:
        self.root = research_root.resolve()
        self.derived = self.root / "derived"
        self.store = ClimateStore(database)
        self.workers = workers
        self.selection = _read_json(self.derived / "model_selection.json")
        self.selected = {
            (row["market_type"], int(row["lead"])): row["candidate"] for row in self.selection["selection"]
        }
        self.eligible = set(self.selection["eligible_stations"])
        self.calibration = self._load_calibration()
        self.weights, self.weight_source = self._load_weights()
        self.markets = self._load_markets()
        self.stations = self._load_stations()
        self.latest_probabilities: dict[str, dict[str, Any]] = {}
        self.station_timezones: dict[str, str] = {}
        self.last_forecast_refresh: datetime | None = None
        self._initialize_meta()

    def close(self) -> None:
        self.store.close()

    def _initialize_meta(self) -> None:
        values = {
            "schema_version": SCHEMA_VERSION,
            "code_version": CODE_VERSION,
            "mode": "RECEIVE_ONLY_SHADOW",
            "wallet_required": False,
            "orders_enabled": False,
            "real_money_enabled": False,
            "thresholds": THRESHOLDS,
            "requested_notional": REQUESTED_NOTIONAL,
            "research_root": str(self.root),
            "selection_sha256": hashlib.sha256((self.derived / "model_selection.json").read_bytes()).hexdigest(),
            "market_universe": len(self.markets),
            "stations": len(self.stations),
            "weight_source": self.weight_source,
            "created_at": iso(utc_now()),
        }
        for key, value in values.items():
            self.store.set_meta(key, value)
        self.store.db.commit()

    def _load_calibration(self) -> dict[tuple[str, str, str, str, int], dict[str, Any]]:
        result: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
        for row in _read_csv(self.derived / "forecast_calibration_train.csv"):
            station = row["station_id"] if row["scope"] == "STATION" else "GLOBAL"
            key = (station, row["market_type"], row["unit"], row["model"], int(row["lead"]))
            result[key] = {"n": int(row["n"]), "bias": float(row["bias"]), "sigma": float(row["sigma"])}
        return result

    def _load_weights(self) -> tuple[dict[tuple[str, str, int, str], float], str]:
        try:
            database, events, _, _ = _load_inputs(self.root)
            points = _forecast_points(database, events)
            local, global_, _ = _calibrate(points, events)
            base = _base_predictions(points, events, local, global_)
            weights = _train_weights(base, events)
            database.close()
            return weights, "REBUILT_FROM_FROZEN_TRAIN_ONLY"
        except Exception:
            return {}, "EQUAL_WEIGHT_FAIL_CLOSED_FALLBACK"

    def _load_markets(self) -> dict[str, dict[str, Any]]:
        contracts = {row["slug"]: row for row in _read_csv(self.derived / "resolution_contracts_enriched.csv")}
        scores = {
            (row["city"], row["market_type"], row["station_id"]): row
            for row in _read_csv(self.derived / "climate_forecastability_scorecard.csv")
        }
        today = utc_now().date()
        result: dict[str, dict[str, Any]] = {}
        for row in _read_csv(self.derived / "markets.csv"):
            contract = contracts.get(row["slug"])
            if not contract or row["split"] != "SHADOW_FORWARD" or row["closed"] != "False":
                continue
            station = contract.get("station_id") or ""
            market_day = date.fromisoformat(row["market_date"])
            if station not in self.eligible or not today <= market_day <= today + timedelta(days=2):
                continue
            strict, reason = strict_contract(contract)
            score = scores.get((row["city"], row["market_type"], station), {})
            result[row["slug"]] = {
                **row,
                "station_id": station,
                "precision": float(contract["precision"]),
                "strict_contract": strict,
                "contract_reason": reason,
                "forecastability_score": _float(score.get("forecastability_score")),
                "sample_grade": score.get("sample_grade") or "UNKNOWN",
                "buckets": json.loads(row["buckets_json"]),
            }
        return result

    def _load_stations(self) -> dict[str, dict[str, str]]:
        wanted = {row["station_id"] for row in self.markets.values()}
        return {row["station_id"]: row for row in _read_csv(self.derived / "station_catalog.csv") if row["station_id"] in wanted}

    def _active_markets(self, observed_at: datetime) -> list[dict[str, Any]]:
        result = []
        for market in self.markets.values():
            timezone_name = self.station_timezones.get(market["station_id"], "UTC")
            try:
                local_day = observed_at.astimezone(ZoneInfo(timezone_name)).date()
            except Exception:
                local_day = observed_at.date()
            market_day = date.fromisoformat(market["market_date"])
            if local_day <= market_day <= local_day + timedelta(days=2):
                result.append(market)
        return result

    def _calibrated(self, market: dict[str, Any], model: str, lead: int, value_c: float) -> dict[str, Any] | None:
        raw_native = _native(value_c, market["unit"])
        local_key = (market["station_id"], market["market_type"], market["unit"], model, lead)
        global_key = ("GLOBAL", market["market_type"], market["unit"], model, lead)
        fit = self.calibration.get(local_key)
        scope = "STATION"
        if not fit or fit["n"] < MIN_LOCAL_CALIBRATION:
            fit = self.calibration.get(global_key)
            scope = "GLOBAL"
        if not fit or fit["n"] < MIN_LOCAL_CALIBRATION:
            return None
        point = raw_native - fit["bias"]
        probabilities = _bucket_probabilities(point, fit["sigma"], tuple(market["buckets"]), market["precision"])
        return {
            "raw_point_c": value_c,
            "raw_point_native": raw_native,
            "point": point,
            "probabilities": probabilities,
            "calibration_scope": scope,
            "bias": fit["bias"],
            "sigma": fit["sigma"],
        }

    def _probability_for_market(
        self, market: dict[str, Any], payload: dict[str, Any], payload_hash: str, observed_at: datetime
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        daily = payload.get("daily") or {}
        dates = [str(value) for value in daily.get("time") or []]
        if market["market_date"] not in dates:
            return None, []
        index = dates.index(market["market_date"])
        timezone_name = str(payload.get("timezone") or "UTC")
        self.station_timezones[market["station_id"]] = timezone_name
        local_today = observed_at.astimezone(ZoneInfo(timezone_name)).date()
        lead = (date.fromisoformat(market["market_date"]) - local_today).days
        candidate = self.selected.get((market["market_type"], lead))
        if not candidate:
            return None, []
        variable = "temperature_2m_max" if market["market_type"] == "HIGHEST" else "temperature_2m_min"
        components: dict[str, dict[str, Any]] = {}
        forecast_rows = []
        for model in OPEN_METEO_MODELS:
            values = daily.get(f"{variable}_{model}") or []
            if index >= len(values) or values[index] is None:
                continue
            calibrated = self._calibrated(market, model, lead, float(values[index]))
            if not calibrated:
                continue
            components[model] = calibrated
            forecast_rows.append(
                {
                    "station_id": market["station_id"], "market_date": market["market_date"], "model": model,
                    "market_type": market["market_type"], "lead_days": lead, "timezone": timezone_name,
                    "payload_hash": payload_hash, **calibrated,
                }
            )
        if candidate == "ensemble_weighted_train":
            weighted = [
                (component, self.weights.get((market["market_type"], market["unit"], lead, model), 1.0))
                for model, component in components.items()
            ]
            if len(weighted) < 2:
                return None, forecast_rows
            total = sum(weight for _, weight in weighted)
            point = sum(component["point"] * weight for component, weight in weighted) / total
            probabilities = tuple(
                sum(component["probabilities"][i] * weight for component, weight in weighted) / total
                for i in range(len(market["buckets"]))
            )
            model_count = len(weighted)
        else:
            component = components.get(candidate)
            if not component:
                return None, forecast_rows
            point = component["point"]
            probabilities = component["probabilities"]
            model_count = 1
        labels = [row["label"] for row in market["buckets"]]
        probability_map = {label: float(value) for label, value in zip(labels, probabilities)}
        top_bucket, top_probability = max(probability_map.items(), key=lambda item: item[1])
        return {
            "candidate": candidate, "lead_days": lead, "point_native": point, "model_count": model_count,
            "top_bucket": top_bucket, "top_probability": top_probability, "probabilities": probability_map,
        }, forecast_rows

    def _fetch_forecast(self, station: dict[str, str]) -> tuple[str, dict[str, Any]]:
        params = {
            "latitude": station["latitude"], "longitude": station["longitude"],
            "elevation": station.get("elevation_m") or "nan",
            "daily": "temperature_2m_max,temperature_2m_min",
            "models": ",".join(OPEN_METEO_MODELS), "timezone": "auto", "past_days": 1, "forecast_days": 4,
        }
        payload = http_json(f"{OPEN_METEO}?{urllib.parse.urlencode(params)}")
        if not isinstance(payload, dict) or not isinstance(payload.get("daily"), dict):
            raise ClimateShadowError(f"Forecast inválido {station['station_id']}")
        return station["station_id"], payload

    def _refresh_forecasts(self, cycle_id: int, markets: list[dict[str, Any]], observed_at: datetime) -> tuple[int, int]:
        payloads: dict[str, dict[str, Any]] = {}
        errors = 0
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self._fetch_forecast, station): station_id for station_id, station in self.stations.items()}
            for future in as_completed(futures):
                station_id = futures[future]
                try:
                    key, payload = future.result()
                    payloads[key] = payload
                except Exception as exc:
                    errors += 1
                    self.store.health(cycle_id, f"forecast:{station_id}", "ERROR", str(exc))
        rows_inserted = 0
        for market in markets:
            payload = payloads.get(market["station_id"])
            if not payload:
                continue
            payload_hash = _hash_json(payload)
            probability, forecast_rows = self._probability_for_market(market, payload, payload_hash, observed_at)
            for row in forecast_rows:
                self.store.db.execute(
                    """INSERT INTO shadow_forecasts(cycle_id,observed_at,station_id,market_date,model,market_type,lead_days,
                    timezone,raw_point_c,raw_point_native,calibrated_point_native,calibration_scope,bias_native,sigma_native,payload_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        cycle_id, iso(observed_at), row["station_id"], row["market_date"], row["model"], row["market_type"],
                        row["lead_days"], row["timezone"], row["raw_point_c"], row["raw_point_native"], row["point"],
                        row["calibration_scope"], row["bias"], row["sigma"], row["payload_hash"],
                    ),
                )
                rows_inserted += 1
            if probability:
                self.latest_probabilities[market["slug"]] = probability
        self.last_forecast_refresh = observed_at
        self.store.health(cycle_id, "open_meteo", "OK" if not errors else "DEGRADED", {"stations": len(payloads), "errors": errors})
        return rows_inserted, errors

    def _fetch_observations(self) -> list[dict[str, Any]]:
        station_ids = sorted(self.stations)
        result: list[dict[str, Any]] = []
        for start in range(0, len(station_ids), 30):
            params = urllib.parse.urlencode({"ids": ",".join(station_ids[start:start + 30]), "format": "json", "hours": 30})
            payload = http_json(f"{AVIATION_WEATHER}?{params}")
            if isinstance(payload, list):
                result.extend(row for row in payload if isinstance(row, dict))
        return result

    def _store_observations(self, cycle_id: int, observed_at: datetime) -> tuple[int, int]:
        try:
            rows = self._fetch_observations()
        except Exception as exc:
            self.store.health(cycle_id, "aviation_weather", "ERROR", str(exc))
            return 0, 1
        inserted = 0
        grouped: defaultdict[tuple[str, str, str], list[tuple[datetime, float]]] = defaultdict(list)
        for row in rows:
            station = str(row.get("icaoId") or "")
            if station not in self.stations or row.get("obsTime") is None:
                continue
            observation_time = datetime.fromtimestamp(float(row["obsTime"]), timezone.utc)
            timezone_name = self.station_timezones.get(station, "UTC")
            local_date = observation_time.astimezone(ZoneInfo(timezone_name)).date().isoformat()
            temperature = _float(row.get("temp"))
            if temperature is not None:
                grouped[(station, local_date, timezone_name)].append((observation_time, temperature))
            raw_hash = _hash_json(row)
            cursor = self.store.db.execute(
                """INSERT OR IGNORE INTO shadow_observations(station_id,observation_time,report_time,receipt_time,temperature_c,
                dewpoint_c,wind_direction,wind_speed_knots,visibility_miles,altimeter_hpa,weather,cover,raw_observation,raw_hash,first_observed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    station, iso(observation_time), row.get("reportTime"), row.get("receiptTime"), temperature,
                    _float(row.get("dewp")), _float(row.get("wdir")), _float(row.get("wspd")), _float(row.get("visib")),
                    _float(row.get("altim")), row.get("wxString"), row.get("cover"), row.get("rawOb"), raw_hash, iso(observed_at),
                ),
            )
            inserted += int(cursor.rowcount > 0)
        for (station, local_date, timezone_name), values in grouped.items():
            latest = max(value[0] for value in values)
            temperatures = [value[1] for value in values]
            self.store.db.execute(
                """INSERT INTO shadow_station_extremes(cycle_id,station_id,local_date,timezone,maximum_c,minimum_c,
                observations,latest_observation_time,age_seconds) VALUES(?,?,?,?,?,?,?,?,?)""",
                (cycle_id, station, local_date, timezone_name, max(temperatures), min(temperatures), len(values), iso(latest), (observed_at - latest).total_seconds()),
            )
        self.store.health(cycle_id, "aviation_weather", "OK", {"downloaded": len(rows), "new": inserted, "extremes": len(grouped)})
        return inserted, 0

    def _fetch_books_chunk(self, tokens: list[str]) -> dict[str, dict[str, Any] | None]:
        if not tokens:
            return {}
        try:
            payload = http_json(f"{CLOB}/books", method="POST", body=[{"token_id": token} for token in tokens])
            result = {token: None for token in tokens}
            if isinstance(payload, list):
                for book in payload:
                    if isinstance(book, dict) and book.get("asset_id") is not None:
                        result[str(book["asset_id"])] = book
            return result
        except Exception:
            if len(tokens) <= 20:
                return {token: None for token in tokens}
            middle = len(tokens) // 2
            return {**self._fetch_books_chunk(tokens[:middle]), **self._fetch_books_chunk(tokens[middle:])}

    def _fetch_books(self, tokens: Iterable[str]) -> dict[str, dict[str, Any] | None]:
        unique = sorted({token for token in tokens if token})
        chunks = [unique[index:index + 250] for index in range(0, len(unique), 250)]
        result: dict[str, dict[str, Any] | None] = {}
        with ThreadPoolExecutor(max_workers=min(self.workers, 6)) as executor:
            for books in executor.map(self._fetch_books_chunk, chunks):
                result.update(books)
        return result

    def _upsert_markets(self, markets: list[dict[str, Any]], observed_at: datetime) -> None:
        for market in markets:
            self.store.db.execute(
                """INSERT INTO shadow_markets(slug,event_id,city,market_type,market_date,station_id,unit,precision,strict_contract,
                contract_reason,forecastability_score,sample_grade,fee_schedule_json,source_type,resolution_source,description_sha256,
                buckets_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                strict_contract=CASE WHEN
                    (shadow_markets.description_sha256 IS NULL OR shadow_markets.description_sha256=excluded.description_sha256)
                    AND (shadow_markets.fee_schedule_json IS NULL OR shadow_markets.fee_schedule_json=excluded.fee_schedule_json)
                    AND shadow_markets.buckets_json=excluded.buckets_json
                    THEN excluded.strict_contract ELSE 0 END,
                contract_reason=CASE WHEN
                    (shadow_markets.description_sha256 IS NULL OR shadow_markets.description_sha256=excluded.description_sha256)
                    AND (shadow_markets.fee_schedule_json IS NULL OR shadow_markets.fee_schedule_json=excluded.fee_schedule_json)
                    AND shadow_markets.buckets_json=excluded.buckets_json
                    THEN excluded.contract_reason ELSE 'IMMUTABLE_MARKET_FIELDS_CHANGED' END,
                fee_schedule_json=COALESCE(shadow_markets.fee_schedule_json,excluded.fee_schedule_json),
                source_type=COALESCE(shadow_markets.source_type,excluded.source_type),
                resolution_source=COALESCE(shadow_markets.resolution_source,excluded.resolution_source),
                description_sha256=COALESCE(shadow_markets.description_sha256,excluded.description_sha256)""",
                (
                    market["slug"], market["event_id"], market["city"], market["market_type"], market["market_date"],
                    market["station_id"], market["unit"], market["precision"], int(market["strict_contract"]),
                    market["contract_reason"], market["forecastability_score"], market["sample_grade"],
                    stable_json(market["fee_schedule"]) if isinstance(market.get("fee_schedule"), dict) else None,
                    market.get("source_type"), market.get("resolution_source"), market.get("description_sha256"),
                    stable_json(market["buckets"]), iso(observed_at), iso(observed_at),
                ),
            )

    def cycle(self, run_id: int, forecast_refresh_seconds: float) -> dict[str, Any]:
        started = time.monotonic()
        observed_at = utc_now()
        cycle_id = self.store.start_cycle(run_id)
        stats = {key: 0 for key in ("active_markets", "forecast_rows", "observation_rows", "quote_rows", "decision_rows", "edge_candidates", "errors")}
        try:
            markets = self._active_markets(observed_at)
            stats["active_markets"] = len(markets)
            self._upsert_markets(markets, observed_at)
            refresh = self.last_forecast_refresh is None or (observed_at - self.last_forecast_refresh).total_seconds() >= forecast_refresh_seconds
            if refresh:
                stats["forecast_rows"], forecast_errors = self._refresh_forecasts(cycle_id, markets, observed_at)
                stats["errors"] += forecast_errors
            stats["observation_rows"], observation_errors = self._store_observations(cycle_id, observed_at)
            stats["errors"] += observation_errors

            tokens = [token for market in markets for bucket in market["buckets"] for token in (bucket.get("yes_token"), bucket.get("no_token"))]
            books = self._fetch_books(tokens)
            book_success = sum(book is not None for book in books.values())
            self.store.health(cycle_id, "clob_books", "OK" if book_success else "ERROR", {"requested": len(set(tokens)), "received": book_success})
            stats["errors"] += int(book_success == 0)

            for market in markets:
                probability = self.latest_probabilities.get(market["slug"])
                if not probability:
                    continue
                cursor = self.store.db.execute(
                    """INSERT INTO shadow_probabilities(cycle_id,slug,observed_at,candidate,lead_days,point_native,model_count,
                    top_bucket,top_probability,probabilities_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        cycle_id, market["slug"], iso(observed_at), probability["candidate"], probability["lead_days"],
                        probability["point_native"], probability["model_count"], probability["top_bucket"],
                        probability["top_probability"], stable_json(probability["probabilities"]),
                    ),
                )
                probability_id = int(cursor.lastrowid)
                quote_map: dict[tuple[str, str], dict[str, Any]] = {}
                for bucket in market["buckets"]:
                    for side, token in (("YES", bucket.get("yes_token")), ("NO", bucket.get("no_token"))):
                        metrics = book_metrics(books.get(str(token)))
                        quote_map[(bucket["label"], side)] = metrics
                        book = books.get(str(token)) or {}
                        self.store.db.execute(
                            """INSERT INTO shadow_quotes(cycle_id,slug,observed_at,bucket,side,token,best_bid,best_ask,bid_size,ask_size,
                            spread,ask_depth_1c_usdc,ask_depth_5c_usdc,vwap_100_usdc,executable_cost_100_usdc,book_timestamp,book_hash)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                cycle_id, market["slug"], iso(observed_at), bucket["label"], side, str(token), metrics["best_bid"],
                                metrics["best_ask"], metrics["bid_size"], metrics["ask_size"], metrics["spread"],
                                metrics["ask_depth_1c_usdc"], metrics["ask_depth_5c_usdc"], metrics["vwap_100_usdc"],
                                metrics["executable_cost_100_usdc"], book.get("timestamp"), book.get("hash"),
                            ),
                        )
                        stats["quote_rows"] += 1
                for threshold in THRESHOLDS:
                    decision = best_decision(
                        probability["probabilities"], quote_map, threshold=threshold, strict=market["strict_contract"],
                        contract_reason=market["contract_reason"], lead_days=probability["lead_days"],
                        fee_schedule=market.get("fee_schedule"),
                    )
                    self.store.db.execute(
                        """INSERT INTO shadow_decisions(probability_id,slug,observed_at,threshold,bucket,side,model_probability,
                        executable_ask,visible_ask_shares,gross_edge,expected_roi_gross,fee_per_share,net_edge,expected_roi_net,
                        would_pass_edge,decision,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            probability_id, market["slug"], iso(observed_at), decision["threshold"], decision["bucket"],
                            decision["side"], decision["model_probability"], decision["executable_ask"],
                            decision["visible_ask_shares"], decision["gross_edge"], decision["expected_roi_gross"],
                            decision["fee_per_share"], decision["net_edge"], decision["expected_roi_net"],
                            decision["would_pass_edge"], decision["decision"], decision["reason"],
                        ),
                    )
                    stats["decision_rows"] += 1
                    stats["edge_candidates"] += decision["would_pass_edge"]
            self.store.finish_cycle(cycle_id, started, "COMPLETE", stats)
            return {"cycle_id": cycle_id, "observed_at": iso(observed_at), "status": "COMPLETE", **stats}
        except Exception as exc:
            stats["errors"] += 1
            error = f"{type(exc).__name__}: {exc}"
            self.store.health(cycle_id, "cycle", "ERROR", error + "\n" + traceback.format_exc()[-4000:])
            self.store.finish_cycle(cycle_id, started, "ERROR", stats, error)
            return {"cycle_id": cycle_id, "observed_at": iso(observed_at), "status": "ERROR", "error": error, **stats}


def write_final_summary(database: Path) -> dict[str, Any]:
    path = database.resolve()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    meta = {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value FROM shadow_meta")}
    run = db.execute("SELECT * FROM shadow_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    totals = dict(db.execute(
        """SELECT COUNT(*) cycles,COALESCE(SUM(status='COMPLETE'),0) complete_cycles,COALESCE(SUM(status='ERROR'),0) error_cycles,
        COALESCE(SUM(forecast_rows),0) forecast_rows,COALESCE(SUM(observation_rows),0) new_observations,
        COALESCE(SUM(quote_rows),0) quote_rows,COALESCE(SUM(decision_rows),0) decision_rows,
        COALESCE(SUM(edge_candidates),0) edge_candidates FROM shadow_cycles"""
    ).fetchone())
    book_coverage = db.execute("SELECT AVG(best_ask IS NOT NULL) FROM shadow_quotes").fetchone()[0]
    forecast_coverage = db.execute(
        "SELECT COUNT(DISTINCT slug)*1.0/NULLIF((SELECT COUNT(*) FROM shadow_markets),0) FROM shadow_probabilities"
    ).fetchone()[0]
    strict_markets = db.execute("SELECT COUNT(*) FROM shadow_markets WHERE strict_contract=1").fetchone()[0]
    decisions = [dict(row) for row in db.execute(
        """SELECT d.slug,m.city,m.market_type,m.station_id,d.observed_at,d.threshold,d.bucket,d.side,d.model_probability,
        d.executable_ask,d.gross_edge,d.expected_roi_gross,d.fee_per_share,d.net_edge,d.expected_roi_net,d.decision,d.reason
        FROM shadow_decisions d JOIN shadow_markets m ON m.slug=d.slug
        WHERE d.would_pass_edge=1 ORDER BY d.gross_edge DESC LIMIT 25"""
    )]
    latest = [dict(row) for row in db.execute(
        """SELECT p.slug,m.city,m.market_type,m.market_date,m.station_id,p.observed_at,p.candidate,p.lead_days,
        p.point_native,p.top_bucket,p.top_probability
        FROM shadow_probabilities p JOIN shadow_markets m ON m.slug=p.slug
        JOIN (SELECT slug,MAX(probability_id) id FROM shadow_probabilities GROUP BY slug) x ON x.id=p.probability_id
        ORDER BY p.top_probability DESC LIMIT 25"""
    )]
    db.close()
    payload = {
        "schema": SCHEMA_VERSION,
        "generated_at": iso(utc_now()),
        "database": str(path),
        "meta": meta,
        "latest_run": dict(run) if run else None,
        "totals": totals,
        "book_coverage": book_coverage,
        "forecast_market_coverage": forecast_coverage,
        "strict_contract_markets": strict_markets,
        "real_money_allowed": False,
        "economic_interpretation": "SHADOW_NET_EDGE_WHEN_FEE_SCHEDULE_VERIFIED",
        "top_edge_candidates": decisions,
        "latest_probabilities": latest,
    }
    output = path.with_name(path.stem + "_final.json")
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def monitor_status(database: Path) -> dict[str, Any]:
    path = database.resolve()
    if not path.exists():
        return {"exists": False, "database": str(path)}
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    meta = {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value FROM shadow_meta")}
    run = db.execute("SELECT * FROM shadow_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    cycle = db.execute("SELECT * FROM shadow_cycles ORDER BY cycle_id DESC LIMIT 1").fetchone()
    totals = dict(db.execute(
        """SELECT COUNT(*) cycles,COALESCE(SUM(forecast_rows),0) forecast_rows,COALESCE(SUM(observation_rows),0) observations,
        COALESCE(SUM(quote_rows),0) quotes,COALESCE(SUM(decision_rows),0) decisions,COALESCE(SUM(edge_candidates),0) edge_candidates
        FROM shadow_cycles"""
    ).fetchone())
    db.close()
    return {
        "exists": True, "database": str(path), "meta": meta, "latest_run": dict(run) if run else None,
        "latest_cycle": dict(cycle) if cycle else None, "totals": totals,
    }


def run_monitor(
    database: Path,
    research_root: Path,
    *,
    end_at: datetime,
    poll_seconds: float,
    forecast_refresh_seconds: float,
    workers: int,
) -> dict[str, Any]:
    if end_at <= utc_now():
        raise ClimateShadowError("end_at debe estar en el futuro")
    if poll_seconds < 60:
        raise ClimateShadowError("poll_seconds mínimo: 60")
    stop_file = database.resolve().with_suffix(database.suffix + ".stop")
    if stop_file.exists():
        stop_file.unlink()
    with MonitorLock(database):
        monitor = ClimateShadowMonitor(database, research_root, workers=workers)
        run_id = monitor.store.start_run(end_at, poll_seconds, forecast_refresh_seconds)
        next_cycle_at = time.monotonic()
        cycles = 0
        errors = 0
        try:
            while utc_now() < end_at and not stop_file.exists():
                result = monitor.cycle(run_id, forecast_refresh_seconds)
                cycles += 1
                errors += int(result["status"] == "ERROR")
                print(stable_json(result), flush=True)
                next_cycle_at += poll_seconds
                now_monotonic = time.monotonic()
                if next_cycle_at < now_monotonic:
                    missed = math.floor((now_monotonic - next_cycle_at) / poll_seconds) + 1
                    next_cycle_at += missed * poll_seconds
                remaining = (end_at - utc_now()).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(max(0.0, next_cycle_at - now_monotonic), remaining))
            status = "STOPPED_BY_MARKER" if stop_file.exists() else "COMPLETE"
            monitor.store.finish_run(run_id, status)
            result = {"run_id": run_id, "status": status, "cycles": cycles, "errors": errors}
        except KeyboardInterrupt:
            monitor.store.finish_run(run_id, "INTERRUPTED")
            result = {"run_id": run_id, "status": "INTERRUPTED", "cycles": cycles, "errors": errors}
        except Exception as exc:
            monitor.store.finish_run(run_id, "ERROR", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            monitor.close()
        result["final_summary"] = write_final_summary(database)
        return result


def create_stop_marker(database: Path) -> dict[str, Any]:
    path = database.resolve().with_suffix(database.suffix + ".stop")
    path.write_text(iso(utc_now()), encoding="utf-8")
    return {"stop_marker": str(path), "created": True}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collector Polymarket Climate receive-only shadow; nunca envía órdenes")
    parser.add_argument("command", choices=("once", "run", "status", "stop", "finalize"))
    parser.add_argument("--database", default=str(DEFAULT_DB))
    parser.add_argument("--research-root", default=str(DEFAULT_RESEARCH_ROOT))
    parser.add_argument("--end-at", help="ISO-8601 absoluto; requerido para run")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--forecast-refresh-seconds", type=float, default=DEFAULT_FORECAST_REFRESH_SECONDS)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    database = Path(args.database)
    research_root = Path(args.research_root)
    if args.command == "status":
        result = monitor_status(database)
    elif args.command == "stop":
        result = create_stop_marker(database)
    elif args.command == "finalize":
        result = write_final_summary(database)
    elif args.command == "once":
        monitor = ClimateShadowMonitor(database, research_root, workers=args.workers)
        run_id = monitor.store.start_run(utc_now() + timedelta(minutes=10), args.poll_seconds, args.forecast_refresh_seconds)
        try:
            result = monitor.cycle(run_id, 0.0)
            monitor.store.finish_run(run_id, "COMPLETE" if result["status"] == "COMPLETE" else "ERROR", result.get("error"))
        finally:
            monitor.close()
    else:
        if not args.end_at:
            raise ClimateShadowError("--end-at es obligatorio para run")
        parsed_end = parse_dt(args.end_at)
        if parsed_end is None:
            raise ClimateShadowError("--end-at inválido")
        result = run_monitor(
            database, research_root, end_at=parsed_end, poll_seconds=args.poll_seconds,
            forecast_refresh_seconds=args.forecast_refresh_seconds, workers=args.workers,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

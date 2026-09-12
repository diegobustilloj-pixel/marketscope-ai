from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import shutil
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Awaitable, Callable, Sequence

import joblib
import numpy as np
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from polymarket_bot.config import Settings
from polymarket_bot.discovery import DiscoveryError, parse_market_payload
from polymarket_bot.domain import MarketDefinition, parse_message_metadata
from polymarket_bot.phase2 import (
    QUALITY_INVALID_BBA,
    QUALITY_MISSING_BINANCE,
    QUALITY_MISSING_CHAINLINK,
    QUALITY_MISSING_DOWN_BBA,
    QUALITY_MISSING_UP_BBA,
    GammaResolutionClient,
    MarketAccumulator,
    ResolutionInfo,
    SilverBuilder,
    SilverMarket,
    SecondBucket,
)
from polymarket_bot.phase3 import GoldMarket, _feature_row
from polymarket_bot.phase4 import (
    DEFAULT_FEE_RATE,
    DEFAULT_SLIPPAGE_PER_SHARE,
    MODEL_FEATURES,
    MODEL_SCOPES,
    FittedModel,
    _build_estimator,
    _feature_matrix,
    _fit_calibrator,
    _model_rows,
    _probability_metrics,
    _taker_cost_per_share,
    _targets,
)


SHADOW_SCHEMA_VERSION = "4"
SHADOW_ARTIFACT_VERSION = "1"
SHADOW_CODE_VERSION = "0.9.4a1"
SHADOW_TWAP_TOPIC = "crypto_prices_twap_thirty"
SHADOW_TWAP_WINDOW_SECONDS = 30
SHADOW_TWAP_MAX_AGE_MS = 5_000
SHADOW_RTDS_WATCHDOG_SECONDS = 12.0
SHADOW_HEALTH_INTERVAL_SECONDS = 60.0
SHADOW_LEGACY_STRIKE_MODEL_ENABLED = False
SHADOW_HORIZON_SECONDS = 60
SHADOW_DIAGNOSTIC_HORIZONS = (120, 30, 15)
SHADOW_TARGET_HOURS = 168.0
SHADOW_MAX_DATABASE_GB = 1.0
SHADOW_MIN_FREE_GB = 20.0
SHADOW_MIN_MARKET_COVERAGE = 0.90
SHADOW_MIN_FEATURE_COVERAGE = 0.85
SHADOW_MIN_RESOLUTION_COVERAGE = 0.90
SHADOW_MIN_TRADES = 100
SHADOW_FAMILY_ALPHA = 0.05
SHADOW_MAX_BRIER_DEGRADATION = 0.01
SHADOW_RESOLUTION_WAIT_SECONDS = 900.0

SHADOW_HYPOTHESES = (
    {
        "model_name": "twap_transfer_strike_hgb",
        "minimum_edge": 0.10,
        "scope": "twap_open_fresh",
    },
)

SHADOW_TRANSFER_BASE_MODELS = {
    "twap_transfer_strike_hgb": "strike_hist_gradient_boosting",
}


SHADOW_DDL = """
CREATE TABLE IF NOT EXISTS shadow_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS shadow_markets (
    condition_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    event_id TEXT,
    market_start_ms INTEGER NOT NULL,
    market_end_ms INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    price_to_beat REAL,
    has_official_strike INTEGER NOT NULL,
    final_price REAL,
    strike_source TEXT,
    strike_fetch_status TEXT NOT NULL,
    event_metadata_json TEXT,
    feature_status TEXT NOT NULL,
    feature_error TEXT,
    label TEXT,
    label_verified INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    resolution_error TEXT
);

CREATE TABLE IF NOT EXISTS shadow_features (
    condition_id TEXT PRIMARY KEY,
    decision_timestamp_ms INTEGER NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    quality_flags INTEGER NOT NULL,
    feature_json TEXT NOT NULL,
    FOREIGN KEY(condition_id) REFERENCES shadow_markets(condition_id)
);

CREATE TABLE IF NOT EXISTS shadow_signals (
    condition_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    scope TEXT NOT NULL,
    minimum_edge REAL,
    probability_up REAL NOT NULL,
    side TEXT,
    expected_edge REAL,
    would_trade INTEGER NOT NULL,
    entry_cost REAL,
    fill_price REAL,
    fee REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(condition_id, model_name),
    FOREIGN KEY(condition_id) REFERENCES shadow_markets(condition_id)
);

CREATE TABLE IF NOT EXISTS shadow_diagnostics (
    condition_id TEXT NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    decision_timestamp_ms INTEGER,
    quality_flags INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    feature_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(condition_id, horizon_seconds),
    FOREIGN KEY(condition_id) REFERENCES shadow_markets(condition_id)
);

CREATE TABLE IF NOT EXISTS shadow_twap_ticks (
    source_timestamp_ms INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    message_timestamp_ms INTEGER,
    symbol TEXT NOT NULL,
    value REAL NOT NULL,
    full_accuracy_value TEXT,
    window_s INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shadow_twap_timestamp
    ON shadow_twap_ticks(source_timestamp_ms);

CREATE TABLE IF NOT EXISTS shadow_health (
    recorded_at TEXT PRIMARY KEY,
    database_bytes INTEGER NOT NULL,
    free_bytes INTEGER NOT NULL,
    counters_json TEXT NOT NULL,
    connections_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shadow_markets_start
    ON shadow_markets(market_start_ms);
CREATE INDEX IF NOT EXISTS idx_shadow_signals_model
    ON shadow_signals(model_name, would_trade);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(
        value,
        timezone.utc,
    ).isoformat(timespec="seconds")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _database_footprint(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (
            path,
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
        )
        if candidate.exists()
    )


def _read_meta(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, Any]:
    return {
        str(row[0]): json.loads(row[1])
        for row in connection.execute(
            f"SELECT key,value FROM {table} ORDER BY key"
        )
    }


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    return connection


def prepare_shadow_models(
    *,
    gold_db: str | Path,
    phase4_db: str | Path,
    output_model: str | Path,
) -> dict[str, Any]:
    gold = Path(gold_db).expanduser().resolve()
    phase4 = Path(phase4_db).expanduser().resolve()
    output = Path(output_model).expanduser().resolve()
    partial = output.with_name(f"{output.name}.partial")
    for source, name in ((gold, "Gold v2"), (phase4, "Fase 4")):
        if not source.is_file():
            raise ValueError(f"No se encontró {name}: {source}")
    for candidate in (output, partial):
        if candidate.exists():
            raise ValueError(
                f"La salida ya existe y no será sobrescrita: {candidate}"
            )

    gold_size_before = gold.stat().st_size
    phase4_size_before = phase4.stat().st_size
    phase4_connection = _open_read_only(phase4)
    try:
        phase4_quick_check = str(
            phase4_connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]
        )
        phase4_meta = _read_meta(
            phase4_connection,
            "phase4_meta",
        )
    finally:
        phase4_connection.close()
    if phase4_meta.get("test_accessed") is not False:
        raise ValueError(
            "El test histórico ya no figura bloqueado; se detuvo el proceso"
        )
    if phase4_meta.get("strategy_selected_on_validation") is not False:
        raise ValueError(
            "El estado de selección Fase 4 no coincide con la auditoría"
        )

    gold_connection = _open_read_only(gold)
    fitted_models: dict[str, FittedModel] = {}
    training_summary: dict[str, Any] = {}
    try:
        gold_quick_check = str(
            gold_connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        gold_meta = _read_meta(gold_connection, "gold_meta")
        rows: list[dict[str, Any]] = []
        columns = tuple(
            dict.fromkeys(
                (
                    "source_dataset",
                    "condition_id",
                    "slug",
                    "market_start_ms",
                    "decision_timestamp_ms",
                    "horizon_seconds",
                    "split",
                    "y_up",
                    "up_best_ask",
                    "down_best_ask",
                    "implied_up_mid_probability",
                    "has_official_strike",
                    "distance_to_strike_bps",
                )
                + tuple(
                    feature
                    for item in MODEL_FEATURES.values()
                    for feature in item
                )
            )
        )
        query_columns = ",".join(columns)
        for row in gold_connection.execute(
            f"""
            SELECT {query_columns}
            FROM gold_features
            WHERE split IN ('train','validation')
              AND horizon_seconds=?
            ORDER BY decision_timestamp_ms,source_dataset,condition_id
            """,
            (SHADOW_HORIZON_SECONDS,),
        ):
            rows.append({column: row[column] for column in columns})
        for hypothesis in SHADOW_HYPOTHESES:
            model_name = str(hypothesis["model_name"])
            training_model_name = SHADOW_TRANSFER_BASE_MODELS.get(
                model_name, model_name
            )
            model_rows = _model_rows(rows, training_model_name)
            if len(model_rows) < 200:
                raise ValueError(
                    f"Datos insuficientes para congelar {model_name}"
                )
            calibration_count = max(50, int(len(model_rows) * 0.20))
            fit_rows = model_rows[:-calibration_count]
            calibration_rows = model_rows[-calibration_count:]
            y_fit = _targets(fit_rows)
            y_calibration = _targets(calibration_rows)
            if (
                len(np.unique(y_fit)) < 2
                or len(np.unique(y_calibration)) < 2
            ):
                raise ValueError(
                    f"Clases insuficientes para congelar {model_name}"
                )
            features = MODEL_FEATURES[training_model_name]
            estimator = _build_estimator(training_model_name)
            estimator.fit(_feature_matrix(fit_rows, features), y_fit)
            raw_calibration = np.asarray(
                estimator.predict_proba(
                    _feature_matrix(calibration_rows, features)
                )[:, 1],
                dtype=float,
            )
            calibrator = _fit_calibrator(
                raw_calibration,
                y_calibration,
            )
            fitted_models[model_name] = FittedModel(
                name=model_name,
                horizon_seconds=SHADOW_HORIZON_SECONDS,
                features=tuple(features),
                estimator=estimator,
                calibrator=calibrator,
            )
            training_summary[model_name] = {
                "scope": hypothesis["scope"],
                "training_model_name": training_model_name,
                "rows": len(model_rows),
                "fit_rows": len(fit_rows),
                "calibration_rows": len(calibration_rows),
                "features": list(features),
            }
    finally:
        gold_connection.close()

    gold_size_after = gold.stat().st_size
    phase4_size_after = phase4.stat().st_size
    if (
        gold_size_before != gold_size_after
        or phase4_size_before != phase4_size_after
        or gold_quick_check != "ok"
        or phase4_quick_check != "ok"
    ):
        raise ValueError(
            "Las fuentes no superaron el control de solo lectura"
        )
    artifact = {
        "artifact_type": "polymarket_shadow_forward_models",
        "artifact_version": SHADOW_ARTIFACT_VERSION,
        "created_at": _utc_now(),
        "historical_test_accessed": False,
        "gold_contract_sha256": gold_meta.get(
            "data_contract_sha256"
        ),
        "phase4_contract_sha256": phase4_meta.get(
            "phase4_contract_sha256"
        ),
        "horizon_seconds": SHADOW_HORIZON_SECONDS,
        "hypotheses": list(SHADOW_HYPOTHESES),
        "models": fitted_models,
        "training_summary": training_summary,
        "fee_rate": DEFAULT_FEE_RATE,
        "slippage_per_share": DEFAULT_SLIPPAGE_PER_SHARE,
        "orders_enabled": False,
        "wallet_required": False,
    }
    partial.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, partial, compress=3)
    loaded = joblib.load(partial)
    if (
        loaded.get("artifact_type") != artifact["artifact_type"]
        or set(loaded.get("models", {})) != set(fitted_models)
    ):
        raise ValueError("El artefacto shadow no superó la verificación")
    partial.replace(output)
    return {
        "passed": True,
        "output_model": str(output),
        "output_bytes": output.stat().st_size,
        "historical_test_accessed": False,
        "gold_opened_read_only": True,
        "phase4_opened_read_only": True,
        "gold_unchanged": gold_size_before == gold_size_after,
        "phase4_unchanged": phase4_size_before == phase4_size_after,
        "hypotheses": list(SHADOW_HYPOTHESES),
        "training_summary": training_summary,
        "orders_enabled": False,
        "wallet_required": False,
    }


class ShadowStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("La base shadow no está abierta")
        return self.connection

    def open(
        self,
        *,
        target_hours: float,
        artifact: dict[str, Any],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SHADOW_DDL)
        self.connection = connection
        meta = _read_meta(connection, "shadow_meta")
        if meta:
            if str(meta.get("schema_version")) != SHADOW_SCHEMA_VERSION:
                raise ValueError(
                    "Esquema shadow no compatible. Fase 4.2 requiere una "
                    "base nueva; no reutilice ni modifique la base Fase 4.1."
                )
            if float(meta.get("target_hours")) != float(target_hours):
                raise ValueError(
                    "La duración no coincide con la prueba existente"
                )
            if meta.get("artifact_created_at") != artifact.get(
                "created_at"
            ):
                raise ValueError(
                    "El modelo no coincide con la prueba existente"
                )
            if meta.get("phase4_contract_sha256") != artifact.get(
                "phase4_contract_sha256"
            ):
                raise ValueError(
                    "El contrato Fase 4 no coincide con la prueba existente"
                )
        else:
            now = time.time()
            values = {
                "schema_version": SHADOW_SCHEMA_VERSION,
                "experiment_started_at": _utc_from_timestamp(now),
                "target_hours": target_hours,
                "target_end_at": _utc_from_timestamp(
                    now + target_hours * 3600
                ),
                "artifact_created_at": artifact.get("created_at"),
                "gold_contract_sha256": artifact.get(
                    "gold_contract_sha256"
                ),
                "phase4_contract_sha256": artifact.get(
                    "phase4_contract_sha256"
                ),
                "historical_test_accessed": False,
                "hypotheses": list(SHADOW_HYPOTHESES),
                "fee_rate": artifact.get("fee_rate"),
                "slippage_per_share": artifact.get(
                    "slippage_per_share"
                ),
                "orders_enabled": False,
                "wallet_required": False,
                "rtds_topics": [
                    "crypto_prices_chainlink",
                    SHADOW_TWAP_TOPIC,
                ],
                "twap_window_seconds": SHADOW_TWAP_WINDOW_SECONDS,
                "twap_max_age_ms": SHADOW_TWAP_MAX_AGE_MS,
                "twap_capture_only": False,
                "legacy_strike_model_enabled": SHADOW_LEGACY_STRIKE_MODEL_ENABLED,
                "twap_transfer_model_enabled": True,
                "twap_transfer_origin_model": "strike_hist_gradient_boosting",
                "twap_transfer_distance_feature": "twap_distance_to_open_bps",
                "twap_retraining_required": False,
                "historical_test_consumed_externally": True,
                "money_real_enabled": False,
                "code_version": SHADOW_CODE_VERSION,
            }
            connection.executemany(
                "INSERT INTO shadow_meta(key,value) VALUES(?,?)",
                [(key, _json(value)) for key, value in values.items()],
            )
            connection.commit()
        self.reconcile_interrupted_state()

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO shadow_meta(key,value) VALUES(?,?)
            """,
            (key, _json(value)),
        )
        self.db.commit()

    def meta(self) -> dict[str, Any]:
        return _read_meta(self.db, "shadow_meta")

    def reconcile_interrupted_state(
        self,
        *,
        now_ms: int | None = None,
    ) -> dict[str, int]:
        timestamp_ms = int(time.time() * 1000) if now_ms is None else now_ms
        stale_runs = self.db.execute(
            """
            UPDATE shadow_runs
            SET finished_at=?,status='INTERRUPTED',
                error=COALESCE(error,'RECOVERED_STALE_RUNNING')
            WHERE status='RUNNING'
            """,
            (_utc_now(),),
        ).rowcount
        stale_features = self.db.execute(
            """
            UPDATE shadow_markets
            SET feature_status='FAILED_INTERRUPTED',
                feature_error=COALESCE(
                    feature_error,
                    'interrupted_before_official_feature'
                )
            WHERE feature_status='PENDING'
              AND market_start_ms<=?
            """,
            (timestamp_ms,),
        ).rowcount
        self.db.commit()
        return {
            "stale_runs_recovered": int(stale_runs),
            "stale_features_recovered": int(stale_features),
        }

    def start_run(self) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO shadow_runs(started_at,status)
            VALUES(?,?)
            """,
            (_utc_now(), "RUNNING"),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        error: str | None,
    ) -> None:
        self.db.execute(
            """
            UPDATE shadow_runs
            SET finished_at=?,status=?,error=?
            WHERE run_id=?
            """,
            (_utc_now(), status, error, run_id),
        )
        self.db.commit()

    def save_market(
        self,
        market: SilverMarket,
        *,
        discovery_metadata: dict[str, Any] | None = None,
    ) -> bool:
        metadata = discovery_metadata or {}
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO shadow_markets(
                condition_id,slug,event_id,market_start_ms,market_end_ms,
                discovered_at,price_to_beat,has_official_strike,
                final_price,strike_source,strike_fetch_status,
                event_metadata_json,feature_status,label_verified
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            (
                market.condition_id,
                market.slug,
                market.event_id,
                market.start_ms,
                market.end_ms,
                _utc_now(),
                market.resolution.price_to_beat,
                int(market.resolution.price_to_beat is not None),
                metadata.get("final_price"),
                metadata.get("strike_source"),
                str(metadata.get("strike_fetch_status") or "MISSING"),
                metadata.get("event_metadata_json"),
                "PENDING",
            ),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def update_event_metadata(
        self,
        *,
        condition_id: str,
        metadata: dict[str, Any],
    ) -> None:
        price_to_beat = metadata.get("price_to_beat")
        self.db.execute(
            """
            UPDATE shadow_markets
            SET price_to_beat=COALESCE(?,price_to_beat),
                has_official_strike=CASE
                    WHEN ? IS NOT NULL THEN 1
                    ELSE has_official_strike
                END,
                final_price=COALESCE(?,final_price),
                strike_source=COALESCE(?,strike_source),
                strike_fetch_status=?,
                event_metadata_json=COALESCE(?,event_metadata_json)
            WHERE condition_id=?
            """,
            (
                price_to_beat,
                price_to_beat,
                metadata.get("final_price"),
                metadata.get("strike_source"),
                str(metadata.get("strike_fetch_status") or "MISSING"),
                metadata.get("event_metadata_json"),
                condition_id,
            ),
        )
        self.db.commit()

    def market_exists(self, slug: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM shadow_markets WHERE slug=?",
                (slug,),
            ).fetchone()
            is not None
        )

    def save_feature_failure(
        self,
        condition_id: str,
        reason: str,
    ) -> None:
        self.db.execute(
            """
            UPDATE shadow_markets
            SET feature_status='FAILED',feature_error=?
            WHERE condition_id=?
            """,
            (reason, condition_id),
        )
        self.db.commit()

    def save_diagnostic_feature(
        self,
        *,
        condition_id: str,
        horizon_seconds: int,
        feature: dict[str, Any] | None,
        error: str | None = None,
    ) -> None:
        if feature is None:
            values = (
                condition_id,
                horizon_seconds,
                None,
                None,
                "FAILED",
                error or "feature_invalida",
                None,
                _utc_now(),
            )
        else:
            values = (
                condition_id,
                horizon_seconds,
                int(feature["decision_timestamp_ms"]),
                int(feature["decision_quality_flags"]),
                "SAVED",
                None,
                _json(feature),
                _utc_now(),
            )
        self.db.execute(
            """
            INSERT OR REPLACE INTO shadow_diagnostics(
                condition_id,horizon_seconds,decision_timestamp_ms,
                quality_flags,status,error,feature_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            values,
        )
        self.db.commit()

    def save_feature_and_signals(
        self,
        *,
        condition_id: str,
        feature: dict[str, Any],
        signals: Sequence[dict[str, Any]],
    ) -> None:
        self.db.execute(
            """
            INSERT INTO shadow_features VALUES(?,?,?,?,?)
            """,
            (
                condition_id,
                int(feature["decision_timestamp_ms"]),
                int(feature["horizon_seconds"]),
                int(feature["decision_quality_flags"]),
                _json(feature),
            ),
        )
        self.db.executemany(
            """
            INSERT INTO shadow_signals VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            [
                (
                    condition_id,
                    signal["model_name"],
                    signal["scope"],
                    signal["minimum_edge"],
                    signal["probability_up"],
                    signal["side"],
                    signal["expected_edge"],
                    int(signal["would_trade"]),
                    signal["entry_cost"],
                    signal["fill_price"],
                    signal["fee"],
                    _utc_now(),
                )
                for signal in signals
            ],
        )
        self.db.execute(
            """
            UPDATE shadow_markets
            SET feature_status='SAVED',feature_error=NULL
            WHERE condition_id=?
            """,
            (condition_id,),
        )
        self.db.commit()

    def save_resolution(
        self,
        *,
        condition_id: str,
        resolution: ResolutionInfo,
    ) -> None:
        self.db.execute(
            """
            UPDATE shadow_markets
            SET label=?,label_verified=?,resolved_at=?,
                resolution_error=?
            WHERE condition_id=?
            """,
            (
                resolution.label if resolution.verified else None,
                int(resolution.verified),
                _utc_now() if resolution.verified else None,
                resolution.error,
                condition_id,
            ),
        )
        self.db.commit()

    def pending_resolutions(self) -> list[tuple[str, str, int]]:
        return [
            (str(row[0]), str(row[1]), int(row[2]))
            for row in self.db.execute(
                """
                SELECT condition_id,slug,market_end_ms
                FROM shadow_markets
                WHERE label_verified=0
                ORDER BY market_end_ms
                """
            )
        ]

    def save_twap_tick(
        self,
        *,
        source_timestamp_ms: int,
        value: float,
        window_s: int,
        symbol: str = "btc/usd",
        full_accuracy_value: str | None = None,
        message_timestamp_ms: int | None = None,
    ) -> bool:
        if symbol.lower() != "btc/usd":
            return False
        if int(window_s) != SHADOW_TWAP_WINDOW_SECONDS:
            return False
        if not math.isfinite(float(value)):
            return False
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO shadow_twap_ticks(
                source_timestamp_ms,received_at,message_timestamp_ms,
                symbol,value,full_accuracy_value,window_s
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                int(source_timestamp_ms),
                _utc_now(),
                int(message_timestamp_ms) if message_timestamp_ms is not None else None,
                symbol.lower(),
                float(value),
                str(full_accuracy_value) if full_accuracy_value is not None else None,
                int(window_s),
            ),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def save_health(
        self,
        *,
        counters: Counter[str],
        connections: dict[str, str],
    ) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO shadow_health VALUES(?,?,?,?,?)
            """,
            (
                _utc_now(),
                _database_footprint(self.path),
                shutil.disk_usage(self.path.parent).free,
                _json(dict(counters)),
                _json(connections),
            ),
        )
        self.db.commit()

    def quick_check(self) -> str:
        return str(self.db.execute("PRAGMA quick_check").fetchone()[0])

    def close(self) -> None:
        if self.connection is not None:
            self.connection.commit()
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            self.connection = None


def _number(value: Any) -> float | None:
    try:
        result = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result if result is None or math.isfinite(result) else None


def _mid(bid: Any, ask: Any) -> float | None:
    if bid is None or ask is None:
        return None
    return (float(bid) + float(ask)) / 2.0


def _spread(bid: Any, ask: Any) -> float | None:
    if bid is None or ask is None:
        return None
    return float(ask) - float(bid)


def _materialize_rows(
    accumulator: MarketAccumulator,
    *,
    through_second: int,
) -> list[dict[str, Any]]:
    current = dict(accumulator.initial_values)
    result: list[dict[str, Any]] = []
    for second in range(through_second + 1):
        bucket = accumulator.buckets.get(second, SecondBucket())
        current.update(bucket.values)
        timestamp_ms = accumulator.market.start_ms + second * 1000
        up_bid = current.get("up_best_bid")
        up_ask = current.get("up_best_ask")
        down_bid = current.get("down_best_bid")
        down_ask = current.get("down_best_ask")
        chainlink = current.get("chainlink_price")
        binance = current.get("binance_price")
        chainlink_ts = current.get("chainlink_timestamp_ms")
        binance_ts = current.get("binance_timestamp_ms")
        up_mid = _mid(up_bid, up_ask)
        down_mid = _mid(down_bid, down_ask)
        flags = 0
        if chainlink is None:
            flags |= QUALITY_MISSING_CHAINLINK
        if up_bid is None or up_ask is None:
            flags |= QUALITY_MISSING_UP_BBA
        if down_bid is None or down_ask is None:
            flags |= QUALITY_MISSING_DOWN_BBA
        if binance is None:
            flags |= QUALITY_MISSING_BINANCE
        if (
            up_bid is not None
            and up_ask is not None
            and float(up_bid) > float(up_ask)
        ) or (
            down_bid is not None
            and down_ask is not None
            and float(down_bid) > float(down_ask)
        ):
            flags |= QUALITY_INVALID_BBA
        result.append(
            {
                "condition_id": accumulator.market.condition_id,
                "second_offset": second,
                "timestamp_ms": timestamp_ms,
                "time_remaining_seconds": 300 - second,
                "chainlink_price": chainlink,
                "chainlink_age_ms": (
                    max(0, timestamp_ms - int(chainlink_ts))
                    if chainlink_ts is not None
                    else None
                ),
                "binance_price": binance,
                "binance_age_ms": (
                    max(0, timestamp_ms - int(binance_ts))
                    if binance_ts is not None
                    else None
                ),
                "up_best_bid": up_bid,
                "up_best_ask": up_ask,
                "up_mid": up_mid,
                "up_spread": _spread(up_bid, up_ask),
                "down_best_bid": down_bid,
                "down_best_ask": down_ask,
                "down_mid": down_mid,
                "down_spread": _spread(down_bid, down_ask),
                "up_bid_depth_1c": current.get("up_bid_depth_1c"),
                "up_ask_depth_1c": current.get("up_ask_depth_1c"),
                "up_bid_depth_5c": current.get("up_bid_depth_5c"),
                "up_ask_depth_5c": current.get("up_ask_depth_5c"),
                "down_bid_depth_1c": current.get(
                    "down_bid_depth_1c"
                ),
                "down_ask_depth_1c": current.get(
                    "down_ask_depth_1c"
                ),
                "down_bid_depth_5c": current.get(
                    "down_bid_depth_5c"
                ),
                "down_ask_depth_5c": current.get(
                    "down_ask_depth_5c"
                ),
                "up_last_trade_price": current.get(
                    "up_last_trade_price"
                ),
                "up_last_trade_size": current.get(
                    "up_last_trade_size"
                ),
                "down_last_trade_price": current.get(
                    "down_last_trade_price"
                ),
                "down_last_trade_size": current.get(
                    "down_last_trade_size"
                ),
                "market_mid_sum": (
                    up_mid + down_mid
                    if up_mid is not None and down_mid is not None
                    else None
                ),
                "market_ask_overround": (
                    float(up_ask) + float(down_ask) - 1.0
                    if up_ask is not None and down_ask is not None
                    else None
                ),
                "price_change_messages": bucket.counters[
                    "price_change_messages"
                ],
                "book_messages": bucket.counters["book_messages"],
                "best_bid_ask_messages": bucket.counters[
                    "best_bid_ask_messages"
                ],
                "polymarket_trade_count": bucket.counters[
                    "polymarket_trade_count"
                ],
                "polymarket_trade_volume": float(
                    bucket.volumes["polymarket_trade_volume"]
                ),
                "chainlink_updates": bucket.counters[
                    "chainlink_updates"
                ],
                "binance_trade_count": bucket.counters[
                    "binance_trade_count"
                ],
                "binance_trade_volume": float(
                    bucket.volumes["binance_trade_volume"]
                ),
                "quality_flags": flags,
            }
        )
    return result


def _fetch_gamma_json_sync(
    settings: Settings,
    path: str,
) -> tuple[dict[str, Any], str]:
    url = f"{settings.gamma_base_url}{path}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "polymarket-quant-bot-shadow/0.9.2",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.http_timeout_seconds,
        ) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise DiscoveryError(f"Gamma HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoveryError(f"Gamma no disponible: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiscoveryError("Gamma devolvió JSON inválido") from exc
    if not isinstance(payload, dict):
        raise DiscoveryError("Gamma devolvió una estructura inesperada")
    return payload, raw


def _event_metadata_details(payload: dict[str, Any]) -> dict[str, Any]:
    candidates: list[tuple[str, Any]] = [
        ("gamma_event.eventMetadata", payload.get("eventMetadata")),
    ]
    events = payload.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        candidates.append(
            (
                "gamma_market.events[0].eventMetadata",
                events[0].get("eventMetadata"),
            )
        )

    source: str | None = None
    metadata: dict[str, Any] | None = None
    for candidate_source, candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                candidate = None
        if isinstance(candidate, dict):
            source = candidate_source
            metadata = candidate
            break

    if metadata is None:
        return {
            "price_to_beat": None,
            "final_price": None,
            "strike_source": None,
            "strike_fetch_status": "MISSING_EVENT_METADATA",
            "event_metadata_json": None,
        }

    price_raw = metadata.get("priceToBeat")
    final_raw = metadata.get("finalPrice")

    def finite_number(value: Any) -> float | None:
        try:
            result = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return result if result is None or math.isfinite(result) else None

    price_to_beat = finite_number(price_raw)
    final_price = finite_number(final_raw)
    if price_to_beat is not None:
        status = "FOUND"
    elif price_raw is not None:
        status = "INVALID_PRICE_TO_BEAT"
    else:
        status = "MISSING_PRICE_TO_BEAT"
    return {
        "price_to_beat": price_to_beat,
        "final_price": final_price,
        "strike_source": source,
        "strike_fetch_status": status,
        "event_metadata_json": _json(metadata),
    }


def _fetch_event_metadata_sync(
    settings: Settings,
    slug: str,
) -> dict[str, Any]:
    escaped = urllib.parse.quote(slug, safe="")
    payload, _ = _fetch_gamma_json_sync(
        settings,
        f"/events/slug/{escaped}",
    )
    return _event_metadata_details(payload)


def _fetch_market_sync(
    settings: Settings,
    slug: str,
) -> tuple[MarketDefinition, ResolutionInfo, dict[str, Any]]:
    escaped = urllib.parse.quote(slug, safe="")
    event_market: MarketDefinition | None = None
    event_metadata: dict[str, Any] | None = None
    event_error: DiscoveryError | None = None
    try:
        event_payload, event_raw = _fetch_gamma_json_sync(
            settings,
            f"/events/slug/{escaped}",
        )
        event_market = parse_market_payload(event_payload, event_raw)
        event_metadata = _event_metadata_details(event_payload)
        if event_metadata["price_to_beat"] is not None:
            return (
                event_market,
                ResolutionInfo(
                    price_to_beat=float(event_metadata["price_to_beat"]),
                    payload_raw=event_raw,
                ),
                event_metadata,
            )
    except DiscoveryError as exc:
        event_error = exc

    try:
        market_payload, market_raw = _fetch_gamma_json_sync(
            settings,
            f"/markets/slug/{escaped}",
        )
    except DiscoveryError:
        if event_market is not None:
            metadata = event_metadata or {
                "price_to_beat": None,
                "final_price": None,
                "strike_source": None,
                "strike_fetch_status": "MISSING_EVENT_METADATA",
                "event_metadata_json": None,
            }
            return event_market, ResolutionInfo(), metadata
        if event_error is not None:
            raise event_error
        raise

    market = event_market or parse_market_payload(market_payload, market_raw)
    resolution = GammaResolutionClient.parse(market_raw)
    market_metadata = _event_metadata_details(market_payload)
    metadata = (
        market_metadata
        if market_metadata["price_to_beat"] is not None
        else event_metadata or market_metadata
    )
    if metadata["price_to_beat"] is not None:
        resolution = replace(
            resolution,
            price_to_beat=float(metadata["price_to_beat"]),
        )
    return market, resolution, metadata


def _silver_market(
    market: MarketDefinition,
    resolution: ResolutionInfo,
) -> SilverMarket:
    prefix = "btc-updown-5m-"
    if not market.slug.startswith(prefix):
        raise DiscoveryError("Slug BTC 5m no compatible")
    start_seconds = int(market.slug.removeprefix(prefix))
    token_by_outcome = market.token_by_outcome
    if "Up" not in token_by_outcome or "Down" not in token_by_outcome:
        raise DiscoveryError("El mercado no contiene tokens Up/Down")
    return SilverMarket(
        condition_id=market.condition_id,
        slug=market.slug,
        event_id=market.event_id,
        question=market.question,
        start_ms=start_seconds * 1000,
        end_ms=(start_seconds + 300) * 1000,
        up_token_id=token_by_outcome["Up"],
        down_token_id=token_by_outcome["Down"],
        resolution_source=market.resolution_source,
        resolution=resolution,
    )


class LiveShadowState:
    def __init__(self) -> None:
        self.builder: SilverBuilder | None = None
        self.market: SilverMarket | None = None
        self.counters: Counter[str] = Counter()
        self.connections: dict[str, str] = {}
        self.latest_chainlink: tuple[float, int] | None = None
        self.latest_binance: tuple[float, int] | None = None
        self.latest_twap: tuple[float, int, int, str | None] | None = None
        self.twap_history: deque[tuple[float, int, int, str | None]] = deque(maxlen=900)

    def twap_at_or_before(
        self,
        timestamp_ms: int,
    ) -> tuple[float, int, int, str | None] | None:
        for item in reversed(self.twap_history):
            if item[1] <= timestamp_ms:
                return item
        return None

    def set_market(self, market: SilverMarket) -> None:
        self.market = market
        self.builder = SilverBuilder(
            [market],
            writer=_NoopSilverWriter(),  # type: ignore[arg-type]
        )
        self.builder.latest_chainlink = self.latest_chainlink
        self.builder.latest_binance = self.latest_binance

    def clear_market(self) -> None:
        self.builder = None
        self.market = None

    def ingest(
        self,
        *,
        source: str,
        default_stream: str,
        raw: str,
    ) -> None:
        stream, timestamp_ms, _, _ = parse_message_metadata(
            raw,
            default_stream,
        )
        self.counters[f"{source}:{stream}"] += 1
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.counters[f"{source}:malformed"] += 1
            return
        if source == "rtds" and isinstance(payload, dict):
            inner = payload.get("payload")
            if isinstance(inner, dict):
                price = _number(inner.get("value"))
                raw_timestamp = inner.get("timestamp")
                if price is not None and raw_timestamp is not None:
                    if stream == "crypto_prices_chainlink":
                        self.latest_chainlink = (
                            price,
                            int(raw_timestamp),
                        )
                    elif stream == SHADOW_TWAP_TOPIC:
                        raw_window = inner.get("window_s")
                        try:
                            window_s = int(raw_window)
                        except (TypeError, ValueError):
                            window_s = 0
                        full_accuracy = inner.get("full_accuracy_value")
                        if window_s == SHADOW_TWAP_WINDOW_SECONDS:
                            item = (
                                price,
                                int(raw_timestamp),
                                window_s,
                                (
                                    str(full_accuracy)
                                    if full_accuracy is not None
                                    else None
                                ),
                            )
                            self.latest_twap = item
                            self.twap_history.append(item)
        elif source == "binance" and isinstance(payload, dict):
            price = _number(payload.get("p"))
            raw_timestamp = payload.get("T") or payload.get("E")
            if price is not None and raw_timestamp is not None:
                self.latest_binance = (price, int(raw_timestamp))
        if self.builder is None:
            return
        received_at = _utc_now()
        self.builder.process_event(
            {
                "source": source,
                "stream": stream,
                "source_timestamp_ms": timestamp_ms,
                "received_at": received_at,
                "payload_raw": raw,
            }
        )


def _build_live_feature(
    *,
    state: LiveShadowState,
    market: SilverMarket,
    horizon_seconds: int,
) -> tuple[dict[str, Any] | None, str | None]:
    builder = state.builder
    if builder is None:
        return None, "builder_no_disponible"
    accumulator = builder.accumulators.get(market.condition_id)
    if accumulator is None:
        return None, "sin_eventos_del_mercado"
    rows = _materialize_rows(
        accumulator,
        through_second=300 - horizon_seconds,
    )
    feature, reason = _feature_row(
        market=GoldMarket(
            source_dataset="shadow_forward",
            source_schema_version=4,
            condition_id=market.condition_id,
            slug=market.slug,
            start_ms=market.start_ms,
            end_ms=market.end_ms,
            label="Down",
            price_to_beat=market.resolution.price_to_beat,
            core_coverage=1.0,
            binance_coverage=1.0,
            split="forward",
        ),
        rows=rows,  # type: ignore[arg-type]
        horizon=horizon_seconds,
    )
    if feature is None:
        if reason == "core_incompleto_en_decision":
            offset = 300 - horizon_seconds
            decision = rows[offset] if 0 <= offset < len(rows) else {}
            required = (
                "chainlink_price",
                "up_best_bid",
                "up_best_ask",
                "up_mid",
                "up_spread",
                "down_best_bid",
                "down_best_ask",
                "down_mid",
                "down_spread",
            )
            missing = [name for name in required if decision.get(name) is None]
            flags = int(decision.get("quality_flags") or 0)
            detail = ",".join(missing) if missing else "none"
            reason = (
                f"core_incompleto_en_decision:quality_flags={flags};"
                f"missing={detail}"
            )
        return None, reason

    decision_ts = int(feature["decision_timestamp_ms"])
    twap = state.twap_at_or_before(decision_ts)
    if twap is None:
        feature.update(
            {
                "twap_30s_available": 0,
                "twap_30s_price": None,
                "twap_30s_timestamp_ms": None,
                "twap_30s_age_ms": None,
                "twap_30s_fresh": 0,
                "twap_30s_max_age_ms": SHADOW_TWAP_MAX_AGE_MS,
                "twap_30s_window_s": SHADOW_TWAP_WINDOW_SECONDS,
                "twap_30s_full_accuracy_value": None,
                "twap_minus_chainlink_bps": None,
            }
        )
    else:
        twap_price, twap_ts, twap_window, full_accuracy = twap
        chainlink_price = _number(feature.get("chainlink_price"))
        twap_age_ms = max(0, decision_ts - twap_ts)
        feature.update(
            {
                "twap_30s_available": 1,
                "twap_30s_price": twap_price,
                "twap_30s_timestamp_ms": twap_ts,
                "twap_30s_age_ms": twap_age_ms,
                "twap_30s_fresh": int(twap_age_ms <= SHADOW_TWAP_MAX_AGE_MS),
                "twap_30s_max_age_ms": SHADOW_TWAP_MAX_AGE_MS,
                "twap_30s_window_s": twap_window,
                "twap_30s_full_accuracy_value": full_accuracy,
                "twap_minus_chainlink_bps": (
                    (twap_price / chainlink_price - 1.0) * 10_000.0
                    if chainlink_price not in {None, 0.0}
                    else None
                ),
            }
        )

    opening_twap = state.twap_at_or_before(int(market.start_ms))
    if opening_twap is None:
        feature.update(
            {
                "twap_open_available": 0,
                "twap_open_price": None,
                "twap_open_timestamp_ms": None,
                "twap_open_age_ms": None,
                "twap_open_fresh": 0,
                "twap_distance_to_open_bps": None,
            }
        )
    else:
        open_price, open_ts, open_window, open_full_accuracy = opening_twap
        open_age_ms = int(market.start_ms) - int(open_ts)
        current_twap_price = _number(feature.get("twap_30s_price"))
        opening_fresh = 0 <= open_age_ms <= SHADOW_TWAP_MAX_AGE_MS
        feature.update(
            {
                "twap_open_available": 1,
                "twap_open_price": float(open_price),
                "twap_open_timestamp_ms": int(open_ts),
                "twap_open_age_ms": open_age_ms,
                "twap_open_fresh": int(opening_fresh),
                "twap_open_window_s": int(open_window),
                "twap_open_full_accuracy_value": open_full_accuracy,
                "twap_distance_to_open_bps": (
                    (current_twap_price / float(open_price) - 1.0) * 10_000.0
                    if opening_fresh
                    and current_twap_price not in {None, 0.0}
                    and float(open_price) != 0.0
                    else None
                ),
            }
        )
    return feature, None


class _NoopSilverWriter:
    def flush_market(self, accumulator: MarketAccumulator) -> None:
        del accumulator


MessageHandler = Callable[[str], Awaitable[None]]
FreshnessPredicate = Callable[[str], bool]


def _is_fresh_twap_message(raw: str) -> bool:
    """True only for a valid BTC 30-second TWAP update."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or payload.get("topic") != SHADOW_TWAP_TOPIC:
        return False
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return False
    symbol = str(inner.get("symbol") or "").lower()
    try:
        window_s = int(inner.get("window_s"))
        timestamp_ms = int(inner.get("timestamp"))
    except (TypeError, ValueError):
        return False
    value = _number(inner.get("value"))
    return (
        symbol == "btc/usd"
        and window_s == SHADOW_TWAP_WINDOW_SECONDS
        and timestamp_ms > 0
        and value is not None
    )


async def _websocket_feed(
    *,
    name: str,
    endpoint: str,
    subscription: dict[str, Any] | None,
    heartbeat_text: str | None,
    heartbeat_seconds: float | None,
    use_proxy: bool,
    reconnect_max_seconds: float,
    stop_event: asyncio.Event,
    state: LiveShadowState,
    handler: MessageHandler,
    freshness_timeout_seconds: float | None = None,
    freshness_predicate: FreshnessPredicate | None = None,
) -> None:
    logger = logging.getLogger(name)
    attempt = 0
    while not stop_event.is_set():
        heartbeat_task: asyncio.Task[Any] | None = None
        watchdog_task: asyncio.Task[Any] | None = None
        stale_triggered = asyncio.Event()
        try:
            async with connect(
                endpoint,
                ping_interval=None,
                open_timeout=12,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
                max_queue=4096,
                user_agent_header="polymarket-quant-bot-shadow/0.9.4a1",
                proxy=True if use_proxy else None,
            ) as websocket:
                if subscription is not None:
                    await websocket.send(
                        json.dumps(
                            subscription,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                state.connections[name] = "CONNECTED"
                attempt = 0
                freshness_clock = {"last": time.monotonic()}

                async def heartbeat() -> None:
                    if heartbeat_text is None or heartbeat_seconds is None:
                        return
                    while True:
                        await asyncio.sleep(heartbeat_seconds)
                        await websocket.send(heartbeat_text)

                async def freshness_watchdog() -> None:
                    if (
                        freshness_timeout_seconds is None
                        or freshness_predicate is None
                    ):
                        return
                    poll_seconds = max(0.01, min(2.0, freshness_timeout_seconds / 4.0))
                    while True:
                        await asyncio.sleep(poll_seconds)
                        age = time.monotonic() - freshness_clock["last"]
                        if age <= freshness_timeout_seconds:
                            continue
                        stale_triggered.set()
                        state.connections[name] = f"STALE:{age:.1f}s"
                        state.counters["rtds:stale_watchdog"] += 1
                        logger.warning(
                            "%s sin TWAP valido durante %.1fs; "
                            "forzando reconexión",
                            name,
                            age,
                        )
                        # A connected-but-silent socket does not necessarily raise.
                        # Closing it forces the outer loop to resubscribe.
                        await websocket.close()
                        return

                if heartbeat_text is not None:
                    heartbeat_task = asyncio.create_task(heartbeat())
                if (
                    freshness_timeout_seconds is not None
                    and freshness_predicate is not None
                ):
                    watchdog_task = asyncio.create_task(freshness_watchdog())

                async for message in websocket:
                    if stop_event.is_set():
                        break
                    raw = (
                        message.decode("utf-8", errors="replace")
                        if isinstance(message, bytes)
                        else str(message)
                    )
                    if freshness_predicate is not None and freshness_predicate(raw):
                        freshness_clock["last"] = time.monotonic()
                        state.counters["rtds:twap_watchdog_fresh"] += 1
                    await handler(raw)

                if stale_triggered.is_set() and not stop_event.is_set():
                    state.connections[name] = "RECONNECTING:STALE_TWAP"
                    state.counters["rtds:stale_reconnects"] += 1
                    # Short deterministic pause prevents a reconnect storm while
                    # still recovering far faster than a 5-minute market interval.
                    await _wait_or_stop(stop_event, 1.0)
        except asyncio.CancelledError:
            state.connections[name] = "CANCELLED"
            raise
        except (ConnectionClosed, OSError, TimeoutError) as exc:
            state.connections[name] = f"RECONNECTING:{type(exc).__name__}"
            attempt += 1
            delay = min(reconnect_max_seconds, 2 ** min(attempt, 6))
            delay *= random.uniform(0.8, 1.2)
            logger.warning(
                "%s desconectado; reconexión en %.1fs",
                name,
                delay,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass
        except Exception as exc:
            state.connections[name] = f"FAILED:{type(exc).__name__}"
            logger.exception("%s falló: %s", name, exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except TimeoutError:
                pass
        finally:
            for task in (heartbeat_task, watchdog_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (heartbeat_task, watchdog_task)
                    if task is not None
                ),
                return_exceptions=True,
            )


def _signal_for_model(
    *,
    feature: dict[str, Any],
    model: FittedModel,
    hypothesis: dict[str, Any],
    fee_rate: float,
    slippage_per_share: float,
) -> dict[str, Any]:
    probability_up = float(model.probabilities([feature])[0])
    up_cost, up_fill, up_fee = _taker_cost_per_share(
        float(feature["up_best_ask"]),
        fee_rate=fee_rate,
        slippage_per_share=slippage_per_share,
    )
    down_cost, down_fill, down_fee = _taker_cost_per_share(
        float(feature["down_best_ask"]),
        fee_rate=fee_rate,
        slippage_per_share=slippage_per_share,
    )
    up_edge = probability_up - up_cost
    down_edge = 1.0 - probability_up - down_cost
    minimum_edge = float(hypothesis["minimum_edge"])
    if max(up_edge, down_edge) < minimum_edge:
        side = None
        expected_edge = max(up_edge, down_edge)
        entry_cost = None
        fill_price = None
        fee = None
    elif up_edge >= down_edge:
        side = "Up"
        expected_edge = up_edge
        entry_cost = up_cost
        fill_price = up_fill
        fee = up_fee
    else:
        side = "Down"
        expected_edge = down_edge
        entry_cost = down_cost
        fill_price = down_fill
        fee = down_fee
    return {
        "model_name": model.name,
        "scope": hypothesis["scope"],
        "minimum_edge": minimum_edge,
        "probability_up": probability_up,
        "side": side,
        "expected_edge": expected_edge,
        "would_trade": side is not None,
        "entry_cost": entry_cost,
        "fill_price": fill_price,
        "fee": fee,
    }


async def _resolve_market(
    *,
    settings: Settings,
    store: ShadowStore,
    condition_id: str,
    slug: str,
    market_end_ms: int,
) -> None:
    delay = max(
        0.0,
        market_end_ms / 1000 + 10.0 - time.time(),
    )
    if delay > 0:
        await asyncio.sleep(delay)
    client = GammaResolutionClient(
        base_url=settings.gamma_base_url,
        timeout_seconds=settings.http_timeout_seconds,
        request_pause_seconds=0.0,
    )
    deadline = time.monotonic() + SHADOW_RESOLUTION_WAIT_SECONDS
    last = ResolutionInfo(error="Resolución pendiente")
    while time.monotonic() < deadline:
        last = await asyncio.to_thread(client.fetch, slug)
        if last.verified:
            store.save_resolution(
                condition_id=condition_id,
                resolution=last,
            )
            try:
                metadata = await asyncio.to_thread(
                    _fetch_event_metadata_sync,
                    settings,
                    slug,
                )
            except DiscoveryError:
                return
            store.update_event_metadata(
                condition_id=condition_id,
                metadata=metadata,
            )
            return
        await asyncio.sleep(30.0)
    store.save_resolution(condition_id=condition_id, resolution=last)
    try:
        metadata = await asyncio.to_thread(
            _fetch_event_metadata_sync,
            settings,
            slug,
        )
    except DiscoveryError:
        return
    store.update_event_metadata(
        condition_id=condition_id,
        metadata=metadata,
    )


async def _wait_or_stop(
    stop_event: asyncio.Event,
    seconds: float,
) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_shadow_forward(
    *,
    settings: Settings,
    model_file: str | Path,
    output_db: str | Path,
    target_hours: float = SHADOW_TARGET_HOURS,
    max_database_gb: float = SHADOW_MAX_DATABASE_GB,
    min_free_gb: float = SHADOW_MIN_FREE_GB,
) -> dict[str, Any]:
    if target_hours <= 0:
        raise ValueError("target_hours debe ser positivo")
    if max_database_gb <= 0 or min_free_gb < 0:
        raise ValueError("Límites de almacenamiento inválidos")
    artifact_path = Path(model_file).expanduser().resolve()
    database = Path(output_db).expanduser().resolve()
    if not artifact_path.is_file():
        raise ValueError(
            f"No se encontró el artefacto shadow: {artifact_path}"
        )
    artifact = joblib.load(artifact_path)
    if (
        artifact.get("artifact_type")
        != "polymarket_shadow_forward_models"
        or artifact.get("historical_test_accessed") is not False
        or artifact.get("orders_enabled") is not False
    ):
        raise ValueError("Artefacto shadow no compatible o inseguro")
    models: dict[str, FittedModel] = dict(artifact["models"])
    if "twap_transfer_strike_hgb" not in models:
        legacy_strike = models.get("strike_hist_gradient_boosting")
        if legacy_strike is None:
            raise ValueError(
                "El artefacto no contiene strike_hist_gradient_boosting para "
                "la transferencia TWAP v0.9.3"
            )
        models["twap_transfer_strike_hgb"] = FittedModel(
            name="twap_transfer_strike_hgb",
            horizon_seconds=legacy_strike.horizon_seconds,
            features=legacy_strike.features,
            estimator=legacy_strike.estimator,
            calibrator=legacy_strike.calibrator,
        )
    store = ShadowStore(database)
    store.open(target_hours=target_hours, artifact=artifact)
    meta = store.meta()
    target_end = _parse_utc(str(meta["target_end_at"])).timestamp()
    experiment_start = _parse_utc(
        str(meta["experiment_started_at"])
    ).timestamp()
    run_id = store.start_run()
    stop_event = asyncio.Event()
    state = LiveShadowState()
    resolution_tasks: set[asyncio.Task[Any]] = set()
    status = "COMPLETED"
    error: str | None = None
    safety_stop_reason: str | None = None

    async def ingest_rtds(raw: str) -> None:
        state.ingest(
            source="rtds",
            default_stream="crypto_price",
            raw=raw,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("topic") != SHADOW_TWAP_TOPIC:
            return
        inner = payload.get("payload")
        if not isinstance(inner, dict):
            state.counters["twap:malformed"] += 1
            return
        value = _number(inner.get("value"))
        raw_timestamp = inner.get("timestamp")
        raw_window = inner.get("window_s")
        try:
            source_timestamp_ms = int(raw_timestamp)
            window_s = int(raw_window)
        except (TypeError, ValueError):
            state.counters["twap:malformed"] += 1
            return
        if value is None:
            state.counters["twap:malformed"] += 1
            return
        message_ts = payload.get("timestamp")
        try:
            message_timestamp_ms = int(message_ts) if message_ts is not None else None
        except (TypeError, ValueError):
            message_timestamp_ms = None
        saved = store.save_twap_tick(
            source_timestamp_ms=source_timestamp_ms,
            value=value,
            window_s=window_s,
            symbol=str(inner.get("symbol") or ""),
            full_accuracy_value=(
                str(inner.get("full_accuracy_value"))
                if inner.get("full_accuracy_value") is not None
                else None
            ),
            message_timestamp_ms=message_timestamp_ms,
        )
        state.counters["twap:saved" if saved else "twap:ignored_or_duplicate"] += 1

    async def ingest_binance(raw: str) -> None:
        state.ingest(
            source="binance",
            default_stream="aggTrade",
            raw=raw,
        )

    global_tasks = [
        asyncio.create_task(
            _websocket_feed(
                name="shadow-rtds",
                endpoint=settings.rtds_ws_url,
                subscription={
                    "action": "subscribe",
                    "subscriptions": [
                        {
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": json.dumps(
                                {"symbol": "btc/usd"},
                                separators=(",", ":"),
                            ),
                        },
                        {
                            "topic": SHADOW_TWAP_TOPIC,
                            "type": "update",
                            "filters": json.dumps(
                                {"symbol": "btc/usd"},
                                separators=(",", ":"),
                            ),
                        },
                    ],
                },
                heartbeat_text="PING",
                heartbeat_seconds=5.0,
                use_proxy=settings.ws_use_proxy,
                reconnect_max_seconds=settings.reconnect_max_seconds,
                stop_event=stop_event,
                state=state,
                handler=ingest_rtds,
                freshness_timeout_seconds=SHADOW_RTDS_WATCHDOG_SECONDS,
                freshness_predicate=_is_fresh_twap_message,
            )
        ),
        asyncio.create_task(
            _websocket_feed(
                name="shadow-binance",
                endpoint=settings.binance_ws_url,
                subscription=None,
                heartbeat_text=None,
                heartbeat_seconds=None,
                use_proxy=settings.ws_use_proxy,
                reconnect_max_seconds=settings.reconnect_max_seconds,
                stop_event=stop_event,
                state=state,
                handler=ingest_binance,
            )
        ),
    ]

    async def health_monitor() -> None:
        nonlocal safety_stop_reason
        while not stop_event.is_set():
            store.save_health(
                counters=state.counters,
                connections=state.connections,
            )
            footprint = _database_footprint(database)
            free = shutil.disk_usage(database.parent).free
            if footprint >= max_database_gb * 1_000_000_000:
                safety_stop_reason = "BASE_ALCANZO_LIMITE"
                stop_event.set()
                return
            if free <= min_free_gb * 1_000_000_000:
                safety_stop_reason = "ESPACIO_LIBRE_INSUFICIENTE"
                stop_event.set()
                return
            await _wait_or_stop(stop_event, SHADOW_HEALTH_INTERVAL_SECONDS)

    health_task = asyncio.create_task(health_monitor())
    try:
        for condition_id, slug, end_ms in store.pending_resolutions():
            task = asyncio.create_task(
                _resolve_market(
                    settings=settings,
                    store=store,
                    condition_id=condition_id,
                    slug=slug,
                    market_end_ms=end_ms,
                )
            )
            resolution_tasks.add(task)
            task.add_done_callback(resolution_tasks.discard)

        while time.time() < target_end and not stop_event.is_set():
            now = time.time()
            current_start = int(now) - (int(now) % 300)
            target_start = (
                current_start
                if now <= current_start + 15
                else current_start + 300
            )
            if target_start + 240 > target_end:
                await _wait_or_stop(
                    stop_event,
                    max(0.0, target_end - time.time()),
                )
                break
            await _wait_or_stop(
                stop_event,
                max(0.0, target_start + 1 - time.time()),
            )
            if stop_event.is_set():
                break
            slug = f"btc-updown-5m-{target_start}"
            if store.market_exists(slug):
                await _wait_or_stop(
                    stop_event,
                    max(1.0, target_start + 300 - time.time()),
                )
                continue

            discovered: tuple[
                MarketDefinition,
                ResolutionInfo,
                dict[str, Any],
            ] | None = None
            discovery_deadline = target_start + 30
            while (
                time.time() < discovery_deadline
                and not stop_event.is_set()
            ):
                try:
                    discovered = await asyncio.to_thread(
                        _fetch_market_sync,
                        settings,
                        slug,
                    )
                    break
                except DiscoveryError:
                    await _wait_or_stop(stop_event, 2.0)
            if discovered is None:
                state.counters["market_discovery_missed"] += 1
                continue
            definition, initial_resolution, discovery_metadata = discovered
            market = _silver_market(definition, initial_resolution)
            store.save_market(
                market,
                discovery_metadata=discovery_metadata,
            )
            state.set_market(market)

            async def ingest_clob(raw: str) -> None:
                state.ingest(
                    source="clob",
                    default_stream="market",
                    raw=raw,
                )

            clob_stop = asyncio.Event()
            clob_task = asyncio.create_task(
                _websocket_feed(
                    name=f"shadow-clob-{market.slug}",
                    endpoint=settings.clob_ws_url,
                    subscription={
                        "assets_ids": [
                            market.up_token_id,
                            market.down_token_id,
                        ],
                        "type": "market",
                        "custom_feature_enabled": True,
                    },
                    heartbeat_text="PING",
                    heartbeat_seconds=10.0,
                    use_proxy=settings.ws_use_proxy,
                    reconnect_max_seconds=(
                        settings.reconnect_max_seconds
                    ),
                    stop_event=clob_stop,
                    state=state,
                    handler=ingest_clob,
                )
            )
            active_market = market
            horizons = tuple(
                sorted(
                    {
                        SHADOW_HORIZON_SECONDS,
                        *SHADOW_DIAGNOSTIC_HORIZONS,
                    },
                    reverse=True,
                )
            )
            for horizon_seconds in horizons:
                decision_at = (
                    active_market.end_ms / 1000 - horizon_seconds
                )
                await _wait_or_stop(
                    stop_event,
                    max(0.0, decision_at - time.time()),
                )
                if stop_event.is_set():
                    break

                if active_market.resolution.price_to_beat is None:
                    try:
                        refreshed = await asyncio.to_thread(
                            _fetch_event_metadata_sync,
                            settings,
                            active_market.slug,
                        )
                    except DiscoveryError:
                        state.counters["strike_refresh_failed"] += 1
                    else:
                        store.update_event_metadata(
                            condition_id=active_market.condition_id,
                            metadata=refreshed,
                        )
                        refreshed_strike = refreshed.get("price_to_beat")
                        if refreshed_strike is not None:
                            active_market = replace(
                                active_market,
                                resolution=replace(
                                    active_market.resolution,
                                    price_to_beat=float(refreshed_strike),
                                ),
                            )
                            state.counters["strike_refresh_found"] += 1

                feature, reason = _build_live_feature(
                    state=state,
                    market=active_market,
                    horizon_seconds=horizon_seconds,
                )
                if horizon_seconds != SHADOW_HORIZON_SECONDS:
                    store.save_diagnostic_feature(
                        condition_id=active_market.condition_id,
                        horizon_seconds=horizon_seconds,
                        feature=feature,
                        error=reason,
                    )
                    continue

                if feature is None:
                    store.save_feature_failure(
                        active_market.condition_id,
                        reason or "feature_invalida",
                    )
                    continue

                signals: list[dict[str, Any]] = [
                    {
                        "model_name": "market_implied",
                        "scope": "all_markets",
                        "minimum_edge": None,
                        "probability_up": float(
                            feature["implied_up_mid_probability"]
                        ),
                        "side": None,
                        "expected_edge": None,
                        "would_trade": False,
                        "entry_cost": None,
                        "fill_price": None,
                        "fee": None,
                    }
                ]
                for hypothesis in SHADOW_HYPOTHESES:
                    model_name = str(hypothesis["model_name"])
                    scoring_feature = feature
                    if model_name == "twap_transfer_strike_hgb":
                        if not (
                            int(feature.get("twap_30s_fresh") or 0) == 1
                            and int(feature.get("twap_open_fresh") or 0) == 1
                            and feature.get("twap_distance_to_open_bps") is not None
                        ):
                            state.counters["twap_transfer_model_skipped"] += 1
                            continue
                        scoring_feature = dict(feature)
                        scoring_feature["distance_to_strike_bps"] = float(
                            feature["twap_distance_to_open_bps"]
                        )
                        state.counters["twap_transfer_model_scored"] += 1
                    signals.append(
                        _signal_for_model(
                            feature=scoring_feature,
                            model=models[model_name],
                            hypothesis=hypothesis,
                            fee_rate=float(artifact["fee_rate"]),
                            slippage_per_share=float(
                                artifact["slippage_per_share"]
                            ),
                        )
                    )
                store.save_feature_and_signals(
                    condition_id=active_market.condition_id,
                    feature=feature,
                    signals=signals,
                )

            await _wait_or_stop(
                stop_event,
                max(0.0, market.end_ms / 1000 + 2 - time.time()),
            )
            clob_stop.set()
            clob_task.cancel()
            await asyncio.gather(clob_task, return_exceptions=True)
            state.connections.pop(f"shadow-clob-{market.slug}", None)
            state.clear_market()
            task = asyncio.create_task(
                _resolve_market(
                    settings=settings,
                    store=store,
                    condition_id=market.condition_id,
                    slug=market.slug,
                    market_end_ms=market.end_ms,
                )
            )
            resolution_tasks.add(task)
            task.add_done_callback(resolution_tasks.discard)

        if safety_stop_reason is not None:
            status = "SAFETY_STOP"
        elif time.time() >= target_end:
            store.set_meta("experiment_completed_at", _utc_now())
            status = "COMPLETED"
        else:
            status = "INTERRUPTED"
    except asyncio.CancelledError:
        status = "INTERRUPTED"
        raise
    except KeyboardInterrupt:
        status = "INTERRUPTED"
    except Exception as exc:
        status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"
        logging.getLogger("shadow-forward").exception(
            "Shadow forward falló"
        )
    finally:
        stop_event.set()
        for task in global_tasks:
            task.cancel()
        health_task.cancel()
        await asyncio.gather(
            *global_tasks,
            health_task,
            return_exceptions=True,
        )
        if status == "COMPLETED" and resolution_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *list(resolution_tasks),
                        return_exceptions=True,
                    ),
                    timeout=SHADOW_RESOLUTION_WAIT_SECONDS + 60,
                )
            except TimeoutError:
                pass
        else:
            for task in resolution_tasks:
                task.cancel()
            await asyncio.gather(
                *list(resolution_tasks),
                return_exceptions=True,
            )
        store.save_health(
            counters=state.counters,
            connections=state.connections,
        )
        store.finish_run(run_id, status=status, error=error)
        quick_check = store.quick_check()
        result_meta = store.meta()
        store.close()

    return {
        "status": status,
        "error": error,
        "safety_stop_reason": safety_stop_reason,
        "database": str(database),
        "database_bytes": _database_footprint(database),
        "quick_check": quick_check,
        "experiment_started_at": result_meta[
            "experiment_started_at"
        ],
        "target_end_at": result_meta["target_end_at"],
        "target_hours": target_hours,
        "elapsed_wall_hours": round(
            (time.time() - experiment_start) / 3600,
            6,
        ),
        "reanudable": status in {"INTERRUPTED", "FAILED"},
        "orders_created": False,
        "wallet_required": False,
        "historical_test_accessed": False,
    }


def _trade_forward_metrics(
    rows: Sequence[sqlite3.Row],
    *,
    confidence_z: float,
) -> dict[str, Any]:
    pnls: list[float] = []
    total_cost = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    wins = 0
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in rows:
        if not int(row["would_trade"]):
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
        max_drawdown = max(max_drawdown, peak - cumulative)
    if not pnls:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": None,
            "net_pnl_per_share_sequence": 0.0,
            "roi_on_cost": None,
            "profit_factor": None,
            "mean_pnl": None,
            "pnl_standard_deviation": None,
            "confidence_z": confidence_z,
            "lower_confidence_bound_mean_pnl": None,
            "max_drawdown": 0.0,
        }
    values = np.asarray(pnls, dtype=float)
    mean = float(np.mean(values))
    standard_deviation = (
        float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    )
    standard_error = (
        standard_deviation / math.sqrt(len(values))
        if len(values) > 1
        else 0.0
    )
    net = float(np.sum(values))
    return {
        "trades": len(values),
        "wins": wins,
        "win_rate": round(wins / len(values), 8),
        "net_pnl_per_share_sequence": round(net, 8),
        "roi_on_cost": (
            round(net / total_cost, 8) if total_cost else None
        ),
        "profit_factor": (
            round(gross_profit / gross_loss, 8)
            if gross_loss
            else None
        ),
        "mean_pnl": round(mean, 8),
        "pnl_standard_deviation": round(standard_deviation, 8),
        "confidence_z": round(confidence_z, 8),
        "lower_confidence_bound_mean_pnl": round(
            mean - confidence_z * standard_error,
            8,
        ),
        "max_drawdown": round(max_drawdown, 8),
    }


def audit_shadow_forward(
    *,
    shadow_db: str | Path,
    phase4_db: str | Path,
) -> dict[str, Any]:
    database = Path(shadow_db).expanduser().resolve()
    phase4 = Path(phase4_db).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"No se encontró la base shadow: {database}")
    if not phase4.is_file():
        raise ValueError(f"No se encontró la base Fase 4: {phase4}")
    phase4_size_before = phase4.stat().st_size
    phase4_connection = _open_read_only(phase4)
    try:
        phase4_quick_check = str(
            phase4_connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]
        )
        phase4_meta = _read_meta(
            phase4_connection,
            "phase4_meta",
        )
    finally:
        phase4_connection.close()
    phase4_size_after = phase4.stat().st_size

    connection = _open_read_only(database)
    try:
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        meta = _read_meta(connection, "shadow_meta")
        markets = int(
            connection.execute(
                "SELECT COUNT(*) FROM shadow_markets"
            ).fetchone()[0]
        )
        features = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM shadow_markets
                WHERE feature_status='SAVED'
                """
            ).fetchone()[0]
        )
        resolved = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM shadow_markets
                WHERE label_verified=1
                """
            ).fetchone()[0]
        )
        first_start, last_start = connection.execute(
            """
            SELECT MIN(market_start_ms),MAX(market_start_ms)
            FROM shadow_markets
            """
        ).fetchone()
        experiment_start = _parse_utc(
            str(meta["experiment_started_at"])
        )
        target_hours = float(meta["target_hours"])
        completed_at = meta.get("experiment_completed_at")
        end_time = (
            _parse_utc(str(completed_at))
            if completed_at
            else datetime.now(timezone.utc)
        )
        elapsed_hours = (
            end_time - experiment_start
        ).total_seconds() / 3600
        phase4_contract_matches = (
            meta.get("phase4_contract_sha256") is not None
            and meta.get("phase4_contract_sha256")
            == phase4_meta.get("phase4_contract_sha256")
        )
        expected_markets = max(1, int(target_hours * 12))
        market_coverage = markets / expected_markets
        feature_coverage = features / markets if markets else 0.0
        resolution_coverage = resolved / markets if markets else 0.0

        family_z = NormalDist().inv_cdf(
            1.0 - SHADOW_FAMILY_ALPHA / len(SHADOW_HYPOTHESES)
        )
        model_results: list[dict[str, Any]] = []
        for hypothesis in SHADOW_HYPOTHESES:
            model_name = str(hypothesis["model_name"])
            rows = connection.execute(
                """
                SELECT s.*,m.label
                FROM shadow_signals AS s
                JOIN shadow_markets AS m
                  ON m.condition_id=s.condition_id
                WHERE s.model_name=? AND m.label_verified=1
                ORDER BY m.market_start_ms
                """,
                (model_name,),
            ).fetchall()
            if not rows:
                model_results.append(
                    {
                        "model_name": model_name,
                        "scope": hypothesis["scope"],
                        "resolved_predictions": 0,
                        "candidate": False,
                        "failures": ["sin_predicciones_resueltas"],
                    }
                )
                continue
            targets = np.asarray(
                [1 if str(row["label"]) == "Up" else 0 for row in rows],
                dtype=int,
            )
            probabilities = np.asarray(
                [float(row["probability_up"]) for row in rows],
                dtype=float,
            )
            probability = _probability_metrics(targets, probabilities)
            market_probabilities: list[float] = []
            market_targets: list[int] = []
            for row in rows:
                benchmark = connection.execute(
                    """
                    SELECT probability_up
                    FROM shadow_signals
                    WHERE condition_id=? AND model_name='market_implied'
                    """,
                    (row["condition_id"],),
                ).fetchone()
                if benchmark is not None:
                    market_probabilities.append(float(benchmark[0]))
                    market_targets.append(
                        1 if str(row["label"]) == "Up" else 0
                    )
            market_probability = _probability_metrics(
                np.asarray(market_targets, dtype=int),
                np.asarray(market_probabilities, dtype=float),
            )
            trading = _trade_forward_metrics(
                rows,
                confidence_z=family_z,
            )
            failures: list[str] = []
            if int(trading["trades"]) < SHADOW_MIN_TRADES:
                failures.append("menos_de_100_trades")
            lower = trading["lower_confidence_bound_mean_pnl"]
            if lower is None or float(lower) <= 0:
                failures.append("lcb_forward_no_positivo")
            if (
                float(probability["brier"])
                > float(market_probability["brier"])
                + SHADOW_MAX_BRIER_DEGRADATION
            ):
                failures.append("brier_peor_que_mercado")
            model_results.append(
                {
                    "model_name": model_name,
                    "scope": hypothesis["scope"],
                    "minimum_edge": hypothesis["minimum_edge"],
                    "resolved_predictions": len(rows),
                    "probability": probability,
                    "market_implied_probability": market_probability,
                    "trading": trading,
                    "candidate": not failures,
                    "failures": failures,
                }
            )
        approved = [
            item for item in model_results if item.get("candidate")
        ]
        twap_retraining_required = bool(
            meta.get("twap_retraining_required", False)
        )
        experiment_complete = completed_at is not None
        technical_passed = (
            quick_check == "ok"
            and phase4_quick_check == "ok"
            and phase4_size_before == phase4_size_after
            and phase4_meta.get("test_accessed") is False
            and phase4_contract_matches
            and experiment_complete
            and elapsed_hours >= target_hours - 0.25
            and market_coverage >= SHADOW_MIN_MARKET_COVERAGE
            and feature_coverage >= SHADOW_MIN_FEATURE_COVERAGE
            and resolution_coverage >= SHADOW_MIN_RESOLUTION_COVERAGE
            and _database_footprint(database)
            <= SHADOW_MAX_DATABASE_GB * 1_000_000_000
        )
        runs = [
            {
                "run_id": int(row[0]),
                "started_at": str(row[1]),
                "finished_at": (
                    str(row[2]) if row[2] is not None else None
                ),
                "status": str(row[3]),
                "error": str(row[4]) if row[4] is not None else None,
            }
            for row in connection.execute(
                """
                SELECT run_id,started_at,finished_at,status,error
                FROM shadow_runs ORDER BY run_id
                """
            )
        ]
    finally:
        connection.close()
    return {
        "technical_passed": technical_passed,
        "experiment_complete": experiment_complete,
        "forward_candidate": (
            bool(approved)
            and technical_passed
            and not twap_retraining_required
        ),
        "strategy_validation_blocked_reason": (
            "TWAP_REGIME_REQUIRES_RETRAINING"
            if twap_retraining_required
            else None
        ),
        "twap_retraining_required": twap_retraining_required,
        "money_real_candidate": False,
        "requires_manual_review_before_live": True,
        "historical_test_status_note": (
            "El holdout historico fue inspeccionado externamente durante "
            "diagnostico y no se usa para seleccionar ni validar v0.9.3."
        ),
        "approved_models": [
            item["model_name"] for item in approved
        ],
        "historical_test_accessed": True,
        "historical_test_accessed_by_phase4_pipeline": (
            phase4_meta.get("test_accessed") is True
        ),
        "historical_test_still_locked": False,
        "phase4_opened_read_only": True,
        "phase4_unchanged": phase4_size_before == phase4_size_after,
        "phase4_contract_matches_frozen": phase4_contract_matches,
        "database": str(database),
        "database_bytes": _database_footprint(database),
        "sqlite_quick_check": quick_check,
        "target_hours": target_hours,
        "elapsed_hours": round(elapsed_hours, 6),
        "expected_markets": expected_markets,
        "markets": markets,
        "market_coverage": round(market_coverage, 8),
        "features": features,
        "feature_coverage": round(feature_coverage, 8),
        "resolved": resolved,
        "resolution_coverage": round(resolution_coverage, 8),
        "first_market_start_ms": first_start,
        "last_market_start_ms": last_start,
        "multiple_testing": {
            "hypotheses": len(SHADOW_HYPOTHESES),
            "family_alpha": SHADOW_FAMILY_ALPHA,
            "confidence_z": family_z,
        },
        "model_results": model_results,
        "runs": runs,
        "orders_created": False,
        "wallet_required": False,
        "kelly_used": False,
        "meta": meta,
    }


def shadow_status(path: str | Path) -> dict[str, Any]:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"No se encontró la base shadow: {database}")
    connection = _open_read_only(database)
    try:
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        meta = _read_meta(connection, "shadow_meta")
        market_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(shadow_markets)")
        }
        has_diagnostics = (
            connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='shadow_diagnostics'
                """
            ).fetchone()
            is not None
        )
        has_twap = (
            connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='shadow_twap_ticks'
                """
            ).fetchone()
            is not None
        )
        counts = {
            "markets": int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_markets"
                ).fetchone()[0]
            ),
            "features": int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_features"
                ).fetchone()[0]
            ),
            "signals": int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_signals"
                ).fetchone()[0]
            ),
            "resolved": int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM shadow_markets
                    WHERE label_verified=1
                    """
                ).fetchone()[0]
            ),
        }
        if "has_official_strike" in market_columns:
            counts["markets_with_official_strike"] = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM shadow_markets
                    WHERE has_official_strike=1
                    """
                ).fetchone()[0]
            )
        if has_diagnostics:
            counts["diagnostic_features"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_diagnostics WHERE status='SAVED'"
                ).fetchone()[0]
            )
            counts["diagnostic_failures"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_diagnostics WHERE status='FAILED'"
                ).fetchone()[0]
            )
        feature_rows = connection.execute(
            "SELECT feature_json FROM shadow_features"
        ).fetchall()
        counts["features_with_official_strike_at_decision"] = sum(
            1
            for row in feature_rows
            if int(json.loads(row[0]).get("has_official_strike") or 0) == 1
        )
        counts["features_with_twap_at_decision"] = sum(
            1
            for row in feature_rows
            if int(json.loads(row[0]).get("twap_30s_available") or 0) == 1
        )
        counts["features_with_fresh_twap_at_decision"] = sum(
            1
            for row in feature_rows
            if (
                lambda item: (
                    int(item.get("twap_30s_fresh") or 0) == 1
                    if "twap_30s_fresh" in item
                    else (
                        item.get("twap_30s_age_ms") is not None
                        and int(item["twap_30s_age_ms"]) <= SHADOW_TWAP_MAX_AGE_MS
                    )
                )
            )(json.loads(row[0]))
        )
        if has_twap:
            twap_summary = connection.execute(
                """
                SELECT COUNT(*),MIN(source_timestamp_ms),MAX(source_timestamp_ms),
                       MIN(window_s),MAX(window_s)
                FROM shadow_twap_ticks
                """
            ).fetchone()
            counts["twap_updates"] = int(twap_summary[0] or 0)
            counts["twap_first_timestamp_ms"] = (
                int(twap_summary[1]) if twap_summary[1] is not None else None
            )
            counts["twap_last_timestamp_ms"] = (
                int(twap_summary[2]) if twap_summary[2] is not None else None
            )
            counts["twap_window_seconds_min"] = (
                int(twap_summary[3]) if twap_summary[3] is not None else None
            )
            counts["twap_window_seconds_max"] = (
                int(twap_summary[4]) if twap_summary[4] is not None else None
            )
            counts["markets_with_twap_coverage"] = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM shadow_markets AS m
                    WHERE EXISTS (
                        SELECT 1 FROM shadow_twap_ticks AS t
                        WHERE t.source_timestamp_ms>=m.market_start_ms
                          AND t.source_timestamp_ms<m.market_end_ms
                    )
                    """
                ).fetchone()[0]
            )
        latest_health = connection.execute(
            """
            SELECT recorded_at,database_bytes,free_bytes,
                   counters_json,connections_json
            FROM shadow_health
            ORDER BY recorded_at DESC LIMIT 1
            """
        ).fetchone()
        runs = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM shadow_runs ORDER BY run_id"
            )
        ]
    finally:
        connection.close()
    return {
        "database": str(database),
        "database_bytes": _database_footprint(database),
        "sqlite_quick_check": quick_check,
        **counts,
        "experiment_started_at": meta.get("experiment_started_at"),
        "target_end_at": meta.get("target_end_at"),
        "experiment_completed_at": meta.get(
            "experiment_completed_at"
        ),
        "latest_health": (
            {
                "recorded_at": str(latest_health[0]),
                "database_bytes": int(latest_health[1]),
                "free_bytes": int(latest_health[2]),
                "counters": json.loads(latest_health[3]),
                "connections": json.loads(latest_health[4]),
            }
            if latest_health is not None
            else None
        ),
        "runs": runs,
        "orders_created": False,
        "wallet_required": False,
    }

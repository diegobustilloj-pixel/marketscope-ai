from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import traceback
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - el proyecto se ejecuta en Windows
    import fcntl

import numpy as np

from .elon_post_count_research import (
    CLOB,
    ET,
    GAMMA,
    USER_HANDLE,
    XTRACKER,
    Bucket,
    _api_data,
    _best_book,
    _bucket_probabilities,
    _collect_book,
    _combine_current_distribution,
    _fee_per_share,
    _forecast_models,
    _request_json,
    _resolved_winner_bucket,
    _rule_signature,
    _slug_from_market_link,
    event_buckets,
    iso,
    parse_dt,
    read_json,
    stable_json,
)


SCHEMA_VERSION = "elon_shadow_forward_v001"
CODE_VERSION = "0.0.1"
MAX_HOURS = 24.0
DEFAULT_POLL_SECONDS = 30.0
THRESHOLDS = (0.05, 0.075, 0.10, 0.15)
PRIMARY_THRESHOLD = 0.05
REQUESTED_NOTIONAL = 100.0
MIN_VISIBLE_SHARES = 5.0
DEFAULT_DB = Path("data/elon_shadow_forward_v001.db")
DEFAULT_RESEARCH_ROOT = Path("data/elon_post_count_v001")


class MonitorLock:
    """Evita que dos procesos escriban simultáneamente en la misma bitácora."""

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
            else:  # pragma: no cover - el proyecto se ejecuta en Windows
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise ShadowMonitorError(
                f"Ya hay un monitor activo para {database_display(self.path)}"
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - el proyecto se ejecuta en Windows
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def database_display(path: Path) -> str:
    return str(path).removesuffix(".lock")


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS shadow_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    target_hours REAL NOT NULL,
    poll_seconds REAL NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS shadow_cycles (
    cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    duration_seconds REAL,
    posts_total INTEGER,
    new_posts INTEGER,
    active_markets INTEGER,
    quotes INTEGER,
    decisions INTEGER,
    signals INTEGER,
    error TEXT,
    FOREIGN KEY(run_id) REFERENCES shadow_runs(run_id)
);

CREATE TABLE IF NOT EXISTS shadow_posts (
    platform_id TEXT PRIMARY KEY,
    post_id TEXT,
    created_at TEXT NOT NULL,
    imported_at TEXT,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    first_observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_markets (
    slug TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    tracking_id TEXT NOT NULL,
    title TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    buckets_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    model TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    gamma_count INTEGER,
    count_consistent INTEGER NOT NULL,
    elapsed_hours REAL NOT NULL,
    remaining_hours REAL NOT NULL,
    expected_final REAL NOT NULL,
    median_final INTEGER NOT NULL,
    p10_final INTEGER NOT NULL,
    p90_final INTEGER NOT NULL,
    entropy REAL NOT NULL,
    regime TEXT NOT NULL,
    probabilities_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    FOREIGN KEY(cycle_id) REFERENCES shadow_cycles(cycle_id),
    FOREIGN KEY(slug) REFERENCES shadow_markets(slug)
);

CREATE TABLE IF NOT EXISTS shadow_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    bucket TEXT NOT NULL,
    token TEXT NOT NULL,
    side TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    bid_size REAL NOT NULL,
    ask_size REAL NOT NULL,
    spread REAL,
    book_hash TEXT,
    FOREIGN KEY(snapshot_id) REFERENCES shadow_snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS shadow_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    bucket TEXT NOT NULL,
    side TEXT NOT NULL,
    threshold REAL NOT NULL,
    model_probability REAL NOT NULL,
    executable_ask REAL,
    visible_ask_shares REAL NOT NULL,
    fee_per_share REAL,
    net_edge REAL,
    expected_roi REAL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES shadow_snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS shadow_signals (
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key TEXT NOT NULL UNIQUE,
    snapshot_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    created_at TEXT NOT NULL,
    window_end TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    bucket TEXT NOT NULL,
    side TEXT NOT NULL,
    threshold REAL NOT NULL,
    model_probability REAL NOT NULL,
    executable_ask REAL NOT NULL,
    fee_per_share REAL NOT NULL,
    net_edge REAL NOT NULL,
    expected_roi REAL NOT NULL,
    requested_notional REAL NOT NULL,
    theoretical_shares REAL NOT NULL,
    visible_fill_shares REAL NOT NULL,
    visible_fill_notional REAL NOT NULL,
    capacity_limited INTEGER NOT NULL,
    status TEXT NOT NULL,
    payout REAL,
    fee_usdc REAL,
    pnl_usdc REAL,
    resolved_at TEXT,
    FOREIGN KEY(snapshot_id) REFERENCES shadow_snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS shadow_health (
    health_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER,
    observed_at TEXT NOT NULL,
    component TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    FOREIGN KEY(cycle_id) REFERENCES shadow_cycles(cycle_id)
);

CREATE INDEX IF NOT EXISTS idx_cycles_started ON shadow_cycles(started_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_slug_time ON shadow_snapshots(slug, observed_at);
CREATE INDEX IF NOT EXISTS idx_decisions_snapshot ON shadow_decisions(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_signals_slug_time ON shadow_signals(slug, created_at);
"""


class ShadowMonitorError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class ShadowStore:
    def __init__(self, path: Path, research_root: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "code_version": CODE_VERSION,
            "mode": "RECEIVE_ONLY_SHADOW",
            "wallet_required": False,
            "orders_enabled": False,
            "real_money_enabled": False,
            "thresholds": list(THRESHOLDS),
            "primary_threshold": PRIMARY_THRESHOLD,
            "requested_notional": REQUESTED_NOTIONAL,
            "research_root": str(research_root.resolve()),
            "created_at": iso(_now()),
        }
        existing = dict(self.db.execute("SELECT key,value FROM shadow_meta"))
        if existing and existing.get("schema_version") != _json_value(SCHEMA_VERSION):
            raise ShadowMonitorError("La base shadow tiene un esquema incompatible")
        for key, value in meta.items():
            self.db.execute(
                "INSERT OR IGNORE INTO shadow_meta(key,value) VALUES(?,?)",
                (key, _json_value(value)),
            )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def start_run(self, target_hours: float, poll_seconds: float) -> int:
        cursor = self.db.execute(
            "INSERT INTO shadow_runs(started_at,status,target_hours,poll_seconds) VALUES(?,?,?,?)",
            (iso(_now()), "RUNNING", target_hours, poll_seconds),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE shadow_runs SET ended_at=?,status=?,error=? WHERE run_id=?",
            (iso(_now()), status, error, run_id),
        )
        self.db.commit()

    def start_cycle(self, run_id: int) -> int:
        cursor = self.db.execute(
            "INSERT INTO shadow_cycles(run_id,started_at,status) VALUES(?,?,?)",
            (run_id, iso(_now()), "RUNNING"),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_cycle(self, cycle_id: int, status: str, started: float, stats: dict[str, Any], error: str | None = None) -> None:
        self.db.execute(
            """
            UPDATE shadow_cycles SET finished_at=?,status=?,duration_seconds=?,posts_total=?,new_posts=?,
            active_markets=?,quotes=?,decisions=?,signals=?,error=? WHERE cycle_id=?
            """,
            (
                iso(_now()),
                status,
                time.monotonic() - started,
                stats.get("posts_total"),
                stats.get("new_posts"),
                stats.get("active_markets"),
                stats.get("quotes"),
                stats.get("decisions"),
                stats.get("signals"),
                error,
                cycle_id,
            ),
        )
        self.db.commit()

    def health(self, cycle_id: int | None, component: str, status: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO shadow_health(cycle_id,observed_at,component,status,detail) VALUES(?,?,?,?,?)",
            (cycle_id, iso(_now()), component, status, detail[:4000]),
        )

    def upsert_posts(self, posts: list[dict[str, Any]], observed_at: datetime) -> int:
        inserted = 0
        for post in posts:
            platform_id = str(post.get("platformId") or "")
            if not platform_id:
                continue
            content = str(post.get("content") or "")
            cursor = self.db.execute(
                """
                INSERT OR IGNORE INTO shadow_posts(
                    platform_id,post_id,created_at,imported_at,content_hash,content,first_observed_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    platform_id,
                    str(post.get("id") or ""),
                    iso(parse_dt(post["createdAt"])),
                    iso(parse_dt(post["importedAt"])) if post.get("importedAt") else None,
                    _hash_text(content),
                    content,
                    iso(observed_at),
                ),
            )
            inserted += int(cursor.rowcount > 0)
        return inserted

    def upsert_market(self, tracking: dict[str, Any], event: dict[str, Any], buckets: list[Bucket], observed_at: datetime) -> None:
        description = str(event.get("description") or "")
        self.db.execute(
            """
            INSERT INTO shadow_markets(
                slug,event_id,tracking_id,title,window_start,window_end,rules_hash,buckets_json,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET last_seen_at=excluded.last_seen_at,buckets_json=excluded.buckets_json
            """,
            (
                event["slug"],
                str(event["id"]),
                str(tracking["id"]),
                str(tracking.get("title") or event.get("title") or event["slug"]),
                iso(parse_dt(tracking["startDate"])),
                iso(parse_dt(tracking["endDate"])),
                _hash_text(description),
                stable_json([bucket.__dict__ for bucket in buckets]),
                iso(observed_at),
                iso(observed_at),
            ),
        )

    def insert_snapshot(
        self,
        cycle_id: int,
        slug: str,
        observed_at: datetime,
        model_name: str,
        probabilities: dict[str, float],
        pmf: np.ndarray,
        features: dict[str, Any],
        gamma_count: int | None,
        consistent: bool,
        regime: str,
    ) -> int:
        cdf = np.cumsum(pmf)
        entropy = -sum(value * math.log(value) for value in probabilities.values() if value > 0)
        cursor = self.db.execute(
            """
            INSERT INTO shadow_snapshots(
                cycle_id,slug,observed_at,model,current_count,gamma_count,count_consistent,elapsed_hours,
                remaining_hours,expected_final,median_final,p10_final,p90_final,entropy,regime,
                probabilities_json,features_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cycle_id,
                slug,
                iso(observed_at),
                model_name,
                int(features["current_count"]),
                gamma_count,
                int(consistent),
                float(features["time_elapsed_hours"]),
                float(features["time_remaining_hours"]),
                float(np.dot(np.arange(len(pmf)), pmf)),
                int(np.searchsorted(cdf, 0.5)),
                int(np.searchsorted(cdf, 0.1)),
                int(np.searchsorted(cdf, 0.9)),
                entropy,
                regime,
                stable_json(probabilities),
                stable_json(features),
            ),
        )
        return int(cursor.lastrowid)

    def insert_quote(
        self,
        snapshot_id: int,
        bucket: str,
        token: str,
        side: str,
        book: dict[str, Any] | None,
    ) -> tuple[float | None, float | None, float, float]:
        bid, ask, bid_size, ask_size = _best_book(book)
        self.db.execute(
            """
            INSERT INTO shadow_quotes(snapshot_id,bucket,token,side,best_bid,best_ask,bid_size,ask_size,spread,book_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                bucket,
                token,
                side,
                bid,
                ask,
                bid_size,
                ask_size,
                ask - bid if ask is not None and bid is not None else None,
                _hash_text(stable_json(book)) if book else None,
            ),
        )
        return bid, ask, bid_size, ask_size

    def insert_decision(self, snapshot_id: int, row: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO shadow_decisions(
                snapshot_id,bucket,side,threshold,model_probability,executable_ask,visible_ask_shares,
                fee_per_share,net_edge,expected_roi,decision,reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                row["bucket"],
                row["side"],
                row["threshold"],
                row["model_probability"],
                row["executable_ask"],
                row["visible_ask_shares"],
                row["fee_per_share"],
                row["net_edge"],
                row["expected_roi"],
                row["decision"],
                row["reason"],
            ),
        )

    def insert_signal(
        self,
        snapshot_id: int,
        slug: str,
        window_end: datetime,
        current_count: int,
        row: dict[str, Any],
        observed_at: datetime,
    ) -> bool:
        ask = float(row["executable_ask"])
        theoretical_shares = REQUESTED_NOTIONAL / ask
        visible_shares = min(theoretical_shares, float(row["visible_ask_shares"]))
        key = f"{slug}|{row['bucket']}|{row['side']}|{row['threshold']:.4f}|{current_count}"
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO shadow_signals(
                signal_key,snapshot_id,slug,created_at,window_end,current_count,bucket,side,threshold,
                model_probability,executable_ask,fee_per_share,net_edge,expected_roi,requested_notional,
                theoretical_shares,visible_fill_shares,visible_fill_notional,capacity_limited,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,
                snapshot_id,
                slug,
                iso(observed_at),
                iso(window_end),
                current_count,
                row["bucket"],
                row["side"],
                row["threshold"],
                row["model_probability"],
                ask,
                row["fee_per_share"],
                row["net_edge"],
                row["expected_roi"],
                REQUESTED_NOTIONAL,
                theoretical_shares,
                visible_shares,
                visible_shares * ask,
                int(visible_shares + 1e-9 < theoretical_shares),
                "OPEN_SHADOW",
            ),
        )
        return cursor.rowcount > 0

    def earliest_open_window_start(self) -> datetime | None:
        row = self.db.execute(
            """
            SELECT MIN(m.window_start)
            FROM shadow_signals s
            JOIN shadow_markets m ON m.slug=s.slug
            WHERE s.status='OPEN_SHADOW'
            """
        ).fetchone()
        return parse_dt(row[0]) if row and row[0] else None

    def due_signal_markets(self, observed_at: datetime) -> list[sqlite3.Row]:
        # Cinco minutos de gracia reducen el riesgo de cerrar con datos aún en tránsito.
        cutoff = observed_at - timedelta(minutes=5)
        return list(
            self.db.execute(
                """
                SELECT DISTINCT s.slug,m.event_id,m.window_start,m.window_end,m.buckets_json
                FROM shadow_signals s
                JOIN shadow_markets m ON m.slug=s.slug
                WHERE s.status='OPEN_SHADOW' AND s.window_end<=?
                ORDER BY s.window_end,s.slug
                """,
                (iso(cutoff),),
            )
        )

    def resolve_market_signals(
        self,
        slug: str,
        winning_bucket: str,
        final_count: int,
        observed_at: datetime,
    ) -> int:
        rows = list(
            self.db.execute(
                "SELECT * FROM shadow_signals WHERE slug=? AND status='OPEN_SHADOW'",
                (slug,),
            )
        )
        resolved = 0
        for row in rows:
            yes_won = row["bucket"] == winning_bucket
            payout = float(yes_won if row["side"] == "YES" else not yes_won)
            shares = float(row["visible_fill_shares"])
            fee_usdc = shares * float(row["fee_per_share"])
            pnl = shares * payout - float(row["visible_fill_notional"]) - fee_usdc
            cursor = self.db.execute(
                """
                UPDATE shadow_signals
                SET status='RESOLVED_SHADOW',payout=?,fee_usdc=?,pnl_usdc=?,resolved_at=?
                WHERE signal_id=? AND status='OPEN_SHADOW'
                """,
                (payout, fee_usdc, pnl, iso(observed_at), row["signal_id"]),
            )
            resolved += int(cursor.rowcount > 0)
        self.health(
            None,
            f"resolution:{slug}",
            "OK",
            stable_json(
                {
                    "winner_bucket": winning_bucket,
                    "final_count": final_count,
                    "signals_resolved": resolved,
                }
            ),
        )
        return resolved


def _load_frozen_posts(research_root: Path) -> list[dict[str, Any]]:
    path = research_root / "raw" / "xtracker_posts.json"
    if not path.exists():
        raise ShadowMonitorError(f"Falta el historial congelado: {path}")
    posts = read_json(path)
    if not isinstance(posts, list) or len(posts) < 1000:
        raise ShadowMonitorError("Historial congelado inválido o insuficiente")
    return posts


def _merge_posts(frozen: list[dict[str, Any]], recent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {str(row.get("platformId") or row.get("id")): row for row in frozen}
    for row in recent:
        merged[str(row.get("platformId") or row.get("id"))] = row
    return sorted(merged.values(), key=lambda row: parse_dt(row["createdAt"]))


def _fetch_recent_posts(observed_at: datetime, earliest_needed: datetime | None = None) -> list[dict[str, Any]]:
    start_at = observed_at - timedelta(days=3)
    if earliest_needed is not None:
        start_at = min(start_at, earliest_needed)
    params = {
        "platform": "X",
        "startDate": iso(start_at),
        "endDate": iso(observed_at + timedelta(minutes=1)),
    }
    data = _api_data(f"{XTRACKER}/api/users/{USER_HANDLE}/posts?{urllib.parse.urlencode(params)}")
    if not isinstance(data, list):
        raise ShadowMonitorError("XTracker posts no devolvió una lista")
    return data


def _fetch_active_trackings(observed_at: datetime) -> list[dict[str, Any]]:
    data = _api_data(f"{XTRACKER}/api/users/{USER_HANDLE}/trackings")
    if not isinstance(data, list):
        raise ShadowMonitorError("XTracker trackings no devolvió una lista")
    result = []
    for row in data:
        if not row.get("marketLink"):
            continue
        start = parse_dt(row["startDate"])
        end = parse_dt(row["endDate"])
        if start <= observed_at <= end:
            result.append(row)
    return result


def _fetch_event(slug: str) -> dict[str, Any]:
    payload = _request_json(f"{GAMMA}/events?{urllib.parse.urlencode({'slug': slug})}")
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ShadowMonitorError(f"Evento Gamma no disponible: {slug}")
    return payload[0]


def _fetch_gamma_count(event_id: str) -> int | None:
    payload = _request_json(f"{GAMMA}/events/{event_id}/tweet-count")
    if not isinstance(payload, dict) or payload.get("tweetCount") is None:
        return None
    return int(payload["tweetCount"])


def _compatible_rules(event: dict[str, Any]) -> bool:
    signature = _rule_signature(str(event.get("description") or ""))
    return all(
        signature[key]
        for key in ("counts_main_feed", "counts_quote_posts", "counts_reposts", "excludes_replies", "xtracker_primary")
    )


def _fetch_books(tokens: list[str], workers: int = 10) -> dict[str, dict[str, Any] | None]:
    result: dict[str, dict[str, Any] | None] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tokens)))) as pool:
        futures = [pool.submit(_collect_book, token) for token in tokens]
        for future in as_completed(futures):
            token, book, _ = future.result()
            result[token] = book
    return result


def _regime_from_features(features: dict[str, Any]) -> str:
    ratio = float(features.get("recent_baseline_ratio") or 0.0)
    one_hour = float(features.get("rate_1h") or 0.0)
    if one_hour >= 8 or ratio >= 2.5:
        return "BURST_OR_VERY_ACTIVE"
    if ratio >= 1.5:
        return "ACTIVE"
    if ratio <= 0.5:
        return "INACTIVE"
    return "NORMAL"


def evaluate_side(
    *,
    bucket: str,
    side: str,
    probability: float,
    ask: float | None,
    ask_size: float,
    threshold: float,
    fee_rate: float,
    fees_enabled: bool,
    count_consistent: bool,
    remaining_hours: float,
) -> dict[str, Any]:
    fee = _fee_per_share(ask, fee_rate, fees_enabled) if ask is not None else None
    edge = probability - ask - fee if ask is not None and fee is not None else None
    roi = edge / (ask + fee) if edge is not None and ask is not None and fee is not None and ask + fee > 0 else None
    decision = "NO_TRADE"
    reason = "NO_EXECUTABLE_ASK"
    if not count_consistent:
        reason = "COUNT_NOT_SYNCHRONIZED"
    elif remaining_hours <= 0:
        reason = "WINDOW_ENDED"
    elif ask is None:
        reason = "NO_EXECUTABLE_ASK"
    elif not 0.01 <= ask <= 0.99:
        reason = "ASK_OUTSIDE_ALLOWED_RANGE"
    elif ask_size < MIN_VISIBLE_SHARES:
        reason = "INSUFFICIENT_VISIBLE_SIZE"
    elif edge is None or edge < threshold:
        decision = "WAIT" if remaining_hours > 5 / 60 else "NO_TRADE"
        reason = "EDGE_BELOW_THRESHOLD"
    else:
        decision = "SHADOW_SIGNAL"
        reason = "ALL_SHADOW_GATES_PASSED"
    return {
        "bucket": bucket,
        "side": side,
        "threshold": threshold,
        "model_probability": probability,
        "executable_ask": ask,
        "visible_ask_shares": ask_size,
        "fee_per_share": fee,
        "net_edge": edge,
        "expected_roi": roi,
        "decision": decision,
        "reason": reason,
    }


class ElonShadowMonitor:
    def __init__(self, database: Path, research_root: Path, *, workers: int = 10) -> None:
        self.research_root = research_root.resolve()
        self.store = ShadowStore(database, self.research_root)
        self.frozen_posts = _load_frozen_posts(self.research_root)
        selection_path = self.research_root / "analysis" / "model_selection.json"
        if not selection_path.exists():
            raise ShadowMonitorError(f"Falta selección de modelo: {selection_path}")
        self.selection = read_json(selection_path)
        self.workers = workers

    def close(self) -> None:
        self.store.close()

    def resolve_due_signals(
        self,
        cycle_id: int,
        timestamps: Sequence[float],
        observed_at: datetime,
    ) -> int:
        resolved = 0
        for market in self.store.due_signal_markets(observed_at):
            slug = str(market["slug"])
            try:
                event = _fetch_event(slug)
                official_winner = _resolved_winner_bucket(event)
                gamma_count = _fetch_gamma_count(str(market["event_id"]))
                start = parse_dt(market["window_start"]).timestamp()
                end = parse_dt(market["window_end"]).timestamp()
                reconstructed = sum(start <= stamp <= end for stamp in timestamps)
                buckets = [Bucket(**row) for row in json.loads(market["buckets_json"])]
                reconstructed_winner = next(
                    (bucket.label for bucket in buckets if bucket.contains(reconstructed)),
                    None,
                )
                consistent = (
                    gamma_count is not None
                    and gamma_count == reconstructed
                    and official_winner is not None
                    and official_winner == reconstructed_winner
                )
                if not consistent:
                    self.store.health(
                        cycle_id,
                        f"resolution:{slug}",
                        "DEGRADED",
                        stable_json(
                            {
                                "official_winner": official_winner,
                                "reconstructed_winner": reconstructed_winner,
                                "gamma_count": gamma_count,
                                "reconstructed_count": reconstructed,
                            }
                        ),
                    )
                    continue
                resolved += self.store.resolve_market_signals(
                    slug,
                    official_winner,
                    reconstructed,
                    observed_at,
                )
            except Exception as exc:
                self.store.health(
                    cycle_id,
                    f"resolution:{slug}",
                    "ERROR",
                    f"{type(exc).__name__}: {exc}",
                )
        return resolved

    def cycle(self, run_id: int) -> dict[str, Any]:
        started = time.monotonic()
        cycle_id = self.store.start_cycle(run_id)
        stats = {
            "posts_total": 0,
            "new_posts": 0,
            "active_markets": 0,
            "quotes": 0,
            "decisions": 0,
            "signals": 0,
            "resolved_signals": 0,
        }
        observed_at = _now()
        try:
            recent = _fetch_recent_posts(observed_at, self.store.earliest_open_window_start())
            posts = _merge_posts(self.frozen_posts, recent)
            stats["posts_total"] = len(posts)
            stats["new_posts"] = self.store.upsert_posts(recent, observed_at)
            timestamps = [parse_dt(row["createdAt"]).timestamp() for row in posts]
            self.store.health(cycle_id, "xtracker_posts", "OK", f"posts={len(posts)} recent={len(recent)}")
            stats["resolved_signals"] = self.resolve_due_signals(cycle_id, timestamps, observed_at)

            trackings = _fetch_active_trackings(observed_at)
            stats["active_markets"] = len(trackings)
            self.store.health(cycle_id, "xtracker_trackings", "OK", f"active={len(trackings)}")
            for tracking in trackings:
                slug = _slug_from_market_link(str(tracking["marketLink"]))
                if not slug:
                    continue
                event = _fetch_event(slug)
                if not _compatible_rules(event):
                    self.store.health(cycle_id, f"market:{slug}", "BLOCKED", "INCOMPATIBLE_RULES")
                    continue
                buckets = event_buckets(event)
                if len(buckets) < 2:
                    self.store.health(cycle_id, f"market:{slug}", "BLOCKED", "INVALID_BUCKETS")
                    continue
                start = parse_dt(tracking["startDate"])
                end = parse_dt(tracking["endDate"])
                models, features = _forecast_models(timestamps, start, observed_at, end)
                model_name, pmf = _combine_current_distribution(models, self.selection)
                probabilities_list = _bucket_probabilities(pmf, buckets)
                probabilities = {bucket.label: value for bucket, value in zip(buckets, probabilities_list)}
                gamma_count = _fetch_gamma_count(str(event["id"]))
                current_count = int(features["current_count"])
                consistent = gamma_count is not None and gamma_count == current_count
                self.store.upsert_market(tracking, event, buckets, observed_at)
                snapshot_id = self.store.insert_snapshot(
                    cycle_id,
                    slug,
                    observed_at,
                    model_name,
                    probabilities,
                    pmf,
                    features,
                    gamma_count,
                    consistent,
                    _regime_from_features(features),
                )

                tokens = [token for bucket in buckets for token in (bucket.yes_token, bucket.no_token)]
                books = _fetch_books(tokens, workers=self.workers)
                quote_map: dict[tuple[str, str], tuple[float | None, float | None, float, float]] = {}
                for bucket in buckets:
                    quote_map[(bucket.label, "YES")] = self.store.insert_quote(
                        snapshot_id, bucket.label, bucket.yes_token, "YES", books.get(bucket.yes_token)
                    )
                    quote_map[(bucket.label, "NO")] = self.store.insert_quote(
                        snapshot_id, bucket.label, bucket.no_token, "NO", books.get(bucket.no_token)
                    )
                    stats["quotes"] += 2

                decisions: list[dict[str, Any]] = []
                for threshold in THRESHOLDS:
                    threshold_rows: list[dict[str, Any]] = []
                    for bucket in buckets:
                        for side, probability in (("YES", probabilities[bucket.label]), ("NO", 1.0 - probabilities[bucket.label])):
                            _, ask, _, ask_size = quote_map[(bucket.label, side)]
                            decision = evaluate_side(
                                bucket=bucket.label,
                                side=side,
                                probability=probability,
                                ask=ask,
                                ask_size=ask_size,
                                threshold=threshold,
                                fee_rate=bucket.fee_rate,
                                fees_enabled=bucket.fees_enabled,
                                count_consistent=consistent,
                                remaining_hours=float(features["time_remaining_hours"]),
                            )
                            self.store.insert_decision(snapshot_id, decision)
                            decisions.append(decision)
                            threshold_rows.append(decision)
                            stats["decisions"] += 1
                    eligible = [row for row in threshold_rows if row["decision"] == "SHADOW_SIGNAL"]
                    if eligible:
                        best = max(eligible, key=lambda row: float(row["net_edge"]))
                        if self.store.insert_signal(snapshot_id, slug, end, current_count, best, observed_at):
                            stats["signals"] += 1
                best_primary = max(
                    (row for row in decisions if math.isclose(float(row["threshold"]), PRIMARY_THRESHOLD)),
                    key=lambda row: float(row["net_edge"]) if row["net_edge"] is not None else -math.inf,
                    default=None,
                )
                detail = {
                    "count": current_count,
                    "gamma_count": gamma_count,
                    "consistent": consistent,
                    "remaining_h": features["time_remaining_hours"],
                    "most_likely": max(probabilities.items(), key=lambda item: item[1]),
                    "best_primary": best_primary,
                }
                self.store.health(cycle_id, f"market:{slug}", "OK" if consistent else "DEGRADED", stable_json(detail))
            self.store.db.commit()
            self.store.finish_cycle(cycle_id, "COMPLETE", started, stats)
            return {"cycle_id": cycle_id, "observed_at": iso(observed_at), "status": "COMPLETE", **stats}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.store.health(cycle_id, "cycle", "ERROR", error + "\n" + traceback.format_exc()[-3000:])
            self.store.db.commit()
            self.store.finish_cycle(cycle_id, "ERROR", started, stats, error)
            return {"cycle_id": cycle_id, "observed_at": iso(observed_at), "status": "ERROR", "error": error, **stats}


def run_monitor(
    database: Path,
    research_root: Path,
    *,
    target_hours: float,
    poll_seconds: float,
    workers: int = 10,
) -> dict[str, Any]:
    if not 0 < target_hours <= MAX_HOURS:
        raise ShadowMonitorError(f"target_hours debe estar entre 0 y {MAX_HOURS}")
    if poll_seconds < 10:
        raise ShadowMonitorError("poll_seconds mínimo: 10")
    stop_file = database.resolve().with_suffix(database.suffix + ".stop")
    if stop_file.exists():
        stop_file.unlink()
    with MonitorLock(database):
        monitor = ElonShadowMonitor(database, research_root, workers=workers)
        run_id = monitor.store.start_run(target_hours, poll_seconds)
        deadline = time.monotonic() + target_hours * 3600.0
        next_cycle_at = time.monotonic()
        cycles = 0
        errors = 0
        try:
            while time.monotonic() < deadline and not stop_file.exists():
                result = monitor.cycle(run_id)
                cycles += 1
                errors += int(result["status"] == "ERROR")
                print(stable_json(result), flush=True)

                # Mantiene la cadencia por hora de inicio. Si una consulta tarda más
                # que el intervalo, comienza la siguiente enseguida sin solaparla.
                next_cycle_at += poll_seconds
                now = time.monotonic()
                if next_cycle_at < now:
                    missed = math.floor((now - next_cycle_at) / poll_seconds) + 1
                    next_cycle_at += missed * poll_seconds
                remaining = deadline - now
                if remaining <= 0:
                    break
                time.sleep(min(max(0.0, next_cycle_at - now), remaining))
            status = "STOPPED_BY_MARKER" if stop_file.exists() else "COMPLETE"
            monitor.store.finish_run(run_id, status)
            return {"run_id": run_id, "status": status, "cycles": cycles, "errors": errors}
        except KeyboardInterrupt:
            monitor.store.finish_run(run_id, "INTERRUPTED")
            return {"run_id": run_id, "status": "INTERRUPTED", "cycles": cycles, "errors": errors}
        except Exception as exc:
            monitor.store.finish_run(run_id, "ERROR", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            monitor.close()


def monitor_status(database: Path) -> dict[str, Any]:
    path = database.resolve()
    if not path.exists():
        return {"exists": False, "database": str(path)}
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    meta = {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value FROM shadow_meta")}
    run = db.execute("SELECT * FROM shadow_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    cycle = db.execute("SELECT * FROM shadow_cycles ORDER BY cycle_id DESC LIMIT 1").fetchone()
    snapshots = db.execute("SELECT COUNT(*) FROM shadow_snapshots").fetchone()[0]
    signals = db.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0]
    open_signals = db.execute("SELECT COUNT(*) FROM shadow_signals WHERE status='OPEN_SHADOW'").fetchone()[0]
    resolved_signals = db.execute("SELECT COUNT(*) FROM shadow_signals WHERE status='RESOLVED_SHADOW'").fetchone()[0]
    realized_pnl = db.execute(
        "SELECT COALESCE(SUM(pnl_usdc),0.0) FROM shadow_signals WHERE status='RESOLVED_SHADOW'"
    ).fetchone()[0]
    latest = [dict(row) for row in db.execute(
        """
        SELECT s.slug,s.observed_at,s.current_count,s.gamma_count,s.count_consistent,s.remaining_hours,
               s.expected_final,s.median_final,s.p10_final,s.p90_final,s.regime,s.probabilities_json
        FROM shadow_snapshots s
        JOIN (SELECT slug,MAX(snapshot_id) AS snapshot_id FROM shadow_snapshots GROUP BY slug) x
          ON x.snapshot_id=s.snapshot_id
        ORDER BY s.remaining_hours
        """
    )]
    for row in latest:
        row["probabilities"] = json.loads(row.pop("probabilities_json"))
    db.close()
    return {
        "exists": True,
        "database": str(path),
        "meta": meta,
        "latest_run": dict(run) if run else None,
        "latest_cycle": dict(cycle) if cycle else None,
        "snapshots": snapshots,
        "signals": signals,
        "open_signals": open_signals,
        "resolved_signals": resolved_signals,
        "realized_pnl_usdc": realized_pnl,
        "markets": latest,
    }


def create_stop_marker(database: Path) -> dict[str, Any]:
    path = database.resolve().with_suffix(database.suffix + ".stop")
    path.write_text(iso(_now()), encoding="utf-8")
    return {"stop_marker": str(path), "created": True}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor shadow Elon Musk post-count; nunca envía órdenes")
    parser.add_argument("command", choices=("once", "run", "status", "stop"))
    parser.add_argument("--database", default=str(DEFAULT_DB))
    parser.add_argument("--research-root", default=str(DEFAULT_RESEARCH_ROOT))
    parser.add_argument("--target-hours", type=float, default=24.0)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args(argv)
    database = Path(args.database)
    research_root = Path(args.research_root)
    if args.command == "status":
        result = monitor_status(database)
    elif args.command == "stop":
        result = create_stop_marker(database)
    elif args.command == "once":
        monitor = ElonShadowMonitor(database, research_root, workers=args.workers)
        run_id = monitor.store.start_run(0.01, args.poll_seconds)
        try:
            result = monitor.cycle(run_id)
            monitor.store.finish_run(run_id, "COMPLETE" if result["status"] == "COMPLETE" else "ERROR", result.get("error"))
        finally:
            monitor.close()
    else:
        result = run_monitor(
            database,
            research_root,
            target_hours=args.target_hours,
            poll_seconds=args.poll_seconds,
            workers=args.workers,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

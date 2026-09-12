from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


GAMMA_API = "https://gamma-api.polymarket.com"


def _fetch_event(slug: str, attempts: int = 5) -> tuple[str, dict[str, Any] | None, str | None]:
    url = GAMMA_API + "/events?" + urllib.parse.urlencode({"slug": slug, "limit": 10})
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": "PolyLedger-CarForensics/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                return slug, payload[0], None
            return slug, None, "event not found"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(min(10.0, 0.5 * (2**attempt)))
    return slug, None, str(last_error)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_metadata(
            event_slug TEXT PRIMARY KEY,
            event_id TEXT,
            title TEXT,
            start_date TEXT,
            end_date TEXT,
            closed_time TEXT,
            active INTEGER,
            closed INTEGER,
            enable_neg_risk INTEGER,
            tags_json TEXT NOT NULL,
            raw_json TEXT,
            fetched_at INTEGER NOT NULL,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS market_metadata(
            condition_id TEXT PRIMARY KEY,
            event_slug TEXT NOT NULL,
            market_id TEXT,
            slug TEXT,
            question TEXT,
            start_date TEXT,
            end_date TEXT,
            closed_time TEXT,
            active INTEGER,
            closed INTEGER,
            neg_risk INTEGER,
            outcomes_json TEXT NOT NULL,
            outcome_prices_json TEXT NOT NULL,
            token_ids_json TEXT NOT NULL,
            fees_enabled INTEGER,
            maker_base_fee REAL,
            taker_base_fee REAL,
            holding_rewards_enabled INTEGER,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS market_event_slug ON market_metadata(event_slug);
        """
    )
    return connection


def _boolean(value: Any) -> int:
    return 1 if value is True or str(value).lower() == "true" else 0


def _save(connection: sqlite3.Connection, slug: str, event: dict[str, Any] | None, error: str | None) -> None:
    now = int(time.time())
    if event is None:
        connection.execute(
            """INSERT INTO event_metadata(event_slug,tags_json,fetched_at,error)
               VALUES(?,?,?,?)
               ON CONFLICT(event_slug) DO UPDATE SET fetched_at=excluded.fetched_at,error=excluded.error""",
            (slug, "[]", now, error),
        )
        return
    tags = event.get("tags") if isinstance(event.get("tags"), list) else []
    connection.execute(
        """INSERT OR REPLACE INTO event_metadata(
             event_slug,event_id,title,start_date,end_date,closed_time,active,closed,
             enable_neg_risk,tags_json,raw_json,fetched_at,error)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
        (
            slug,
            event.get("id"),
            event.get("title"),
            event.get("startDate") or event.get("creationDate"),
            event.get("endDate"),
            event.get("closedTime"),
            _boolean(event.get("active")),
            _boolean(event.get("closed")),
            _boolean(event.get("enableNegRisk")),
            json.dumps(tags, sort_keys=True, separators=(",", ":")),
            json.dumps(event, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )
    for market in event.get("markets") or []:
        if not isinstance(market, dict) or not market.get("conditionId"):
            continue
        connection.execute(
            """INSERT OR REPLACE INTO market_metadata(
                 condition_id,event_slug,market_id,slug,question,start_date,end_date,
                 closed_time,active,closed,neg_risk,outcomes_json,outcome_prices_json,
                 token_ids_json,fees_enabled,maker_base_fee,taker_base_fee,
                 holding_rewards_enabled,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(market["conditionId"]).lower(),
                slug,
                market.get("id"),
                market.get("slug"),
                market.get("question"),
                market.get("startDate"),
                market.get("endDate"),
                market.get("closedTime"),
                _boolean(market.get("active")),
                _boolean(market.get("closed")),
                _boolean(market.get("negRisk")) or _boolean(event.get("enableNegRisk")),
                str(market.get("outcomes") or "[]"),
                str(market.get("outcomePrices") or "[]"),
                str(market.get("clobTokenIds") or "[]"),
                _boolean(market.get("feesEnabled")),
                market.get("makerBaseFee"),
                market.get("takerBaseFee"),
                _boolean(market.get("holdingRewardsEnabled")),
                json.dumps(market, sort_keys=True, separators=(",", ":")),
            ),
        )


def download(ledger: Path, output: Path, workers: int = 12) -> dict[str, Any]:
    source = sqlite3.connect(ledger)
    slugs = [
        row[0]
        for row in source.execute(
            """SELECT DISTINCT json_extract(raw_json,'$.eventSlug')
               FROM activity_events
               WHERE event_type='TRADE' AND json_extract(raw_json,'$.eventSlug') IS NOT NULL
               ORDER BY 1"""
        )
    ]
    source.close()
    target = _connect(output)
    cached = {row[0] for row in target.execute("SELECT event_slug FROM event_metadata WHERE error IS NULL")}
    pending = [slug for slug in slugs if slug not in cached]
    completed = errors = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_fetch_event, slug): slug for slug in pending}
        for future in as_completed(futures):
            slug, event, error = future.result()
            with target:
                _save(target, slug, event, error)
            completed += 1
            errors += int(error is not None)
            if completed == 1 or completed % 100 == 0 or completed == len(pending):
                print(
                    json.dumps(
                        {
                            "status": "METADATA_PROGRESS",
                            "completed": completed,
                            "pending_total": len(pending),
                            "errors": errors,
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
    result = {
        "status": "COMPLETE",
        "event_slugs_in_ledger": len(slugs),
        "events_cached": target.execute("SELECT COUNT(*) FROM event_metadata WHERE error IS NULL").fetchone()[0],
        "event_errors": target.execute("SELECT COUNT(*) FROM event_metadata WHERE error IS NOT NULL").fetchone()[0],
        "markets_cached": target.execute("SELECT COUNT(*) FROM market_metadata").fetchone()[0],
    }
    target.close()
    print(json.dumps(result, indent=2), flush=True)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Descarga metadatos Gamma para la forensia de @car")
    parser.add_argument("--ledger", default="data/polyledger/car.db")
    parser.add_argument("--output", default="data/car_forensics/car_metadata.db")
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args(argv)
    download(Path(args.ledger), Path(args.output), args.workers)


if __name__ == "__main__":
    main()


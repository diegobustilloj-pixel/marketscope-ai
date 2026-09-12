from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo


GAMMA = "https://gamma-api.polymarket.com"
TAG_SLUG = "daily-temperature"
CUT_OFF = datetime(2026, 9, 2, 0, 8, 41, tzinfo=timezone.utc)
SCHEMA = "polymarket_climate_research_v001"
USER_AGENT = "ProyectoBotV4-ClimateResearch/0.0.1"
TITLE_RE = re.compile(r"^(Highest|Lowest) temperature in (.+?) on (.+?)\?$", re.I)
OPEN_METEO_MODELS = (
    "ecmwf_ifs025",
    "ecmwf_aifs025_single",
    "gfs_seamless",
    "icon_seamless",
    "gem_seamless",
    "jma_seamless",
    "ukmo_seamless",
    "cma_grapes_global",
)


class ClimateResearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class TemperatureBucket:
    label: str
    lower: float | None
    upper: float | None
    unit: str
    market_id: str
    yes_token: str
    no_token: str
    winner: bool

    def contains(self, value: float) -> bool:
        return (self.lower is None or value >= self.lower) and (self.upper is None or value <= self.upper)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def request_json(url: str, attempts: int = 4, timeout: int = 45) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except Exception as exc:  # pragma: no cover - depende de red
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise ClimateResearchError(f"No se pudo consultar {url}: {last}")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(stable_json(row) + "\n")


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_bucket(label: str) -> tuple[float | None, float | None, str]:
    text = label.strip().replace("–", "-").replace(",", "")
    unit_match = re.search(r"°\s*([CF])", text, re.I)
    unit = f"°{unit_match.group(1).upper()}" if unit_match else "UNKNOWN"
    number = r"(-?\d+(?:\.\d+)?)"
    first = re.search(number, text)
    if not first:
        raise ClimateResearchError(f"Bucket climático no reconocido: {label!r}")
    value = float(first.group(1))
    if re.search(r"or\s+below|or\s+lower|≤|below", text, re.I):
        return None, value, unit
    if re.search(r"or\s+above|or\s+higher|≥|above", text, re.I):
        return value, None, unit
    range_match = re.search(number + r"\s*-\s*" + number, text)
    if range_match:
        left, right = float(range_match.group(1)), float(range_match.group(2))
        return min(left, right), max(left, right), unit
    return value, value, unit


def event_buckets(event: dict[str, Any]) -> list[TemperatureBucket]:
    buckets: list[TemperatureBucket] = []
    for market in event.get("markets") or []:
        label = str(market.get("groupItemTitle") or "").strip()
        if not label:
            continue
        lower, upper, unit = parse_bucket(label)
        tokens = [str(token) for token in _json_list(market.get("clobTokenIds"))]
        prices = _json_list(market.get("outcomePrices"))
        try:
            yes_won = bool(prices and float(prices[0]) >= 0.99)
        except (TypeError, ValueError):
            yes_won = False
        buckets.append(
            TemperatureBucket(
                label=label,
                lower=lower,
                upper=upper,
                unit=unit,
                market_id=str(market.get("id") or ""),
                yes_token=tokens[0] if len(tokens) > 0 else "",
                no_token=tokens[1] if len(tokens) > 1 else "",
                winner=yes_won,
            )
        )
    return sorted(
        buckets,
        key=lambda row: (-math.inf if row.lower is None else row.lower, math.inf if row.upper is None else row.upper),
    )


def fetch_daily_temperature_events() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    page_number = 0
    while True:
        params: dict[str, Any] = {
            "limit": 500,
            "tag_slug": TAG_SLUG,
            "order": "id",
            "ascending": "true",
        }
        if cursor:
            params["after_cursor"] = cursor
        url = f"{GAMMA}/events/keyset?{urllib.parse.urlencode(params)}"
        payload = request_json(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ClimateResearchError("Respuesta keyset Gamma inválida")
        batch = payload["events"]
        page_number += 1
        events.extend(row for row in batch if isinstance(row, dict))
        next_cursor = payload.get("next_cursor")
        pages.append({"page": page_number, "rows": len(batch), "next_cursor": next_cursor, "url": url})
        if not batch or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(str(next_cursor))
        cursor = str(next_cursor)
    unique = {str(row.get("id") or row.get("slug")): row for row in events}
    return list(unique.values()), pages


def _title_parts(event: dict[str, Any]) -> tuple[str | None, str | None]:
    match = TITLE_RE.match(str(event.get("title") or "").strip())
    if not match:
        return None, None
    return match.group(1).upper(), match.group(2).strip()


def _source_type(description: str, resolution_source: str) -> str:
    text = f"{description}\n{resolution_source}".lower()
    if "hong kong observatory" in text or "weather.gov.hk" in text:
        return "HONG_KONG_OBSERVATORY"
    if "weather.gov/wrh" in text or "recorded by noaa" in text:
        return "NOAA_NWS"
    if "weather underground" in text or "wunderground.com" in text:
        return "WEATHER_UNDERGROUND"
    if "bom.gov.au" in text or "bureau of meteorology" in text:
        return "AUSTRALIAN_BOM"
    if "environment canada" in text or "weather.gc.ca" in text:
        return "ENVIRONMENT_CANADA"
    return "OTHER"


def parse_resolution_contract(event: dict[str, Any]) -> dict[str, Any]:
    kind, city = _title_parts(event)
    description = str(event.get("description") or "")
    resolution_source = str(event.get("resolutionSource") or "")
    text = f"{description}\n{resolution_source}"
    station_match = re.search(
        r"(?:recorded (?:by [^\n.]+ )?at|recorded at) the (.+? Station)(?: in|,|\.)",
        description,
        re.I,
    )
    if not station_match:
        station_match = re.search(r"recorded by the (.+?)(?: in degrees| on )", description, re.I)
    site_match = re.search(r"[?&]site=([a-z0-9]{3,6})", text, re.I)
    wu_match = re.search(r"wunderground\.com/history/daily/[^\s.]+/([A-Z0-9]{3,6})(?:\.|\s|$)", text, re.I)
    station_id = (site_match or wu_match).group(1).upper() if (site_match or wu_match) else None
    if not station_id and "hong kong observatory" in description.lower():
        station_id = "HKO"
    unit = "°F" if re.search(r"degrees Fahrenheit", description, re.I) else "°C" if re.search(r"degrees Celsius", description, re.I) else None
    decimal_match = re.search(r"to (one|two) decimal place", description, re.I)
    precision = 0.1 if decimal_match and decimal_match.group(1).lower() == "one" else 0.01 if decimal_match else 1.0 if "whole degrees" in description.lower() else None
    fallback = "WEATHER_UNDERGROUND" if re.search(r"Weather Underground.*(?:used|fallback|source)", description, re.I | re.S) else None
    missing_to_lowest = bool(re.search(r"no data.*resolve to the lowest bracket", description, re.I | re.S))
    first_next_day = "first data point for the following date" in description.lower()
    buckets = event_buckets(event)
    units = sorted({bucket.unit for bucket in buckets if bucket.unit != "UNKNOWN"})
    return {
        "slug": event.get("slug"),
        "event_id": str(event.get("id") or ""),
        "market_type": kind,
        "city": city,
        "station_name": station_match.group(1).strip() if station_match else None,
        "station_id": station_id,
        "icao": station_id if station_id and len(station_id) == 4 else None,
        "wmo_id": None,
        "country": None,
        "latitude": None,
        "longitude": None,
        "elevation_m": None,
        "timezone": None,
        "source_type": _source_type(description, resolution_source),
        "resolution_source": resolution_source or None,
        "fallback_source": fallback,
        "unit": unit or (units[0] if len(units) == 1 else None),
        "precision": precision,
        "frequency": "OBSERVATION_TABLE_UNRESOLVED",
        "period_rule": "LOCAL_CALENDAR_DAY",
        "first_next_day_required": first_next_day,
        "missing_resolves_lowest": missing_to_lowest,
        "description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
        "rules_complete": bool(city and kind and station_id and (unit or len(units) == 1) and precision),
    }


def _winner_label(event: dict[str, Any]) -> str | None:
    winners = [bucket.label for bucket in event_buckets(event) if bucket.winner]
    return winners[0] if len(winners) == 1 else None


def infer_market_date(title: str, end_date: str | None) -> str | None:
    """Infer the local calendar date named by the contract, not its UTC close date."""
    match = TITLE_RE.match(str(title or "").strip())
    end = parse_dt(end_date)
    if not match:
        return end.date().isoformat() if end else None
    month_day = match.group(3).strip()
    reference_year = end.year if end else CUT_OFF.year
    candidates = []
    for year in (reference_year - 1, reference_year, reference_year + 1):
        try:
            candidates.append(datetime.strptime(f"{month_day} {year}", "%B %d %Y").date())
        except ValueError:
            continue
    if not candidates:
        return end.date().isoformat() if end else None
    if not end:
        return min(candidates, key=lambda value: abs((value - CUT_OFF.date()).days)).isoformat()
    reference = end.date()
    return min(candidates, key=lambda value: (abs((value - reference).days), value > reference)).isoformat()


def _partition_events(rows: list[dict[str, Any]]) -> dict[str, str]:
    closed = [row for row in rows if row["winner_bucket"] and row["end_date"] and parse_dt(row["end_date"]) < CUT_OFF]
    closed.sort(key=lambda row: (row["market_date"], row["city"] or "", row["market_type"] or "", row["slug"]))
    groups: list[list[dict[str, Any]]] = []
    for row in closed:
        key = (row["market_date"], row["city"], row["market_type"])
        if not groups or (groups[-1][0]["market_date"], groups[-1][0]["city"], groups[-1][0]["market_type"]) != key:
            groups.append([])
        groups[-1].append(row)
    total = len(groups)
    result: dict[str, str] = {}
    for index, group in enumerate(groups):
        fraction = (index + 1) / max(1, total)
        split = "TRAIN" if fraction <= 0.60 else "VALIDATION" if fraction <= 0.80 else "TEST"
        for row in group:
            result[row["slug"]] = split
    return result


def normalize_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    filtered: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    markets: list[dict[str, Any]] = []
    for event in events:
        kind, city = _title_parts(event)
        created = parse_dt(event.get("creationDate") or event.get("createdAt") or event.get("startDate"))
        if kind not in {"HIGHEST", "LOWEST"} or not city or (created and created > CUT_OFF):
            continue
        buckets = event_buckets(event)
        if len(buckets) < 2:
            continue
        contract = parse_resolution_contract(event)
        winner = _winner_label(event)
        filtered.append(event)
        contracts.append(contract)
        markets.append(
            {
                "event_id": str(event.get("id") or ""),
                "slug": str(event.get("slug") or ""),
                "title": str(event.get("title") or ""),
                "market_type": kind,
                "city": city,
                "start_date": event.get("startDate"),
                "created_at": event.get("creationDate") or event.get("createdAt"),
                "end_date": event.get("endDate"),
                "market_date": infer_market_date(str(event.get("title") or ""), event.get("endDate")),
                "closed": bool(event.get("closed")),
                "winner_bucket": winner,
                "bucket_count": len(buckets),
                "unit": contract["unit"],
                "volume": float(event.get("volume") or 0.0),
                "liquidity": float(event.get("liquidity") or 0.0),
                "series_id": str((event.get("series") or [{}])[0].get("id") or ""),
                "series_slug": str((event.get("series") or [{}])[0].get("slug") or ""),
                "buckets_json": stable_json([asdict(bucket) for bucket in buckets]),
            }
        )
    splits = _partition_events(markets)
    for row in markets:
        row["split"] = splits.get(row["slug"], "SHADOW_FORWARD")
    return filtered, contracts, markets


def city_summary(contracts: list[dict[str, Any]], markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contract_by_slug = {str(row["slug"]): row for row in contracts}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for market in markets:
        grouped[(str(market["city"]), str(market["market_type"]))].append(market)
    result: list[dict[str, Any]] = []
    for (city, kind), rows in grouped.items():
        resolved = [row for row in rows if row["winner_bucket"]]
        sources = Counter(str(contract_by_slug[row["slug"]]["source_type"]) for row in rows)
        stations = Counter(str(contract_by_slug[row["slug"]]["station_id"]) for row in rows)
        units = Counter(str(row["unit"]) for row in rows)
        result.append(
            {
                "city": city,
                "market_type": kind,
                "events": len(rows),
                "resolved": len(resolved),
                "train": sum(row["split"] == "TRAIN" for row in rows),
                "validation": sum(row["split"] == "VALIDATION" for row in rows),
                "test": sum(row["split"] == "TEST" for row in rows),
                "shadow_forward": sum(row["split"] == "SHADOW_FORWARD" for row in rows),
                "volume": sum(float(row["volume"]) for row in rows),
                "median_event_volume": sorted(float(row["volume"]) for row in rows)[len(rows) // 2],
                "primary_station_id": stations.most_common(1)[0][0],
                "station_variants": len(stations),
                "primary_source": sources.most_common(1)[0][0],
                "primary_unit": units.most_common(1)[0][0],
                "strong_sample_gate": len(resolved) >= 100,
                "provisional_sample_gate": len(resolved) >= 30,
            }
        )
    return sorted(result, key=lambda row: (-row["resolved"], -row["volume"], row["city"], row["market_type"]))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def fetch_station_catalog(station_ids: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    aviation_ids = sorted({value for value in station_ids if len(value) == 4 and value != "HKO"})
    for index in range(0, len(aviation_ids), 30):
        batch = aviation_ids[index : index + 30]
        params = urllib.parse.urlencode({"ids": ",".join(batch), "format": "json"})
        payload = request_json(f"https://aviationweather.gov/api/data/stationinfo?{params}")
        if isinstance(payload, list):
            rows.extend(row for row in payload if isinstance(row, dict))
        time.sleep(0.7)
    rows.append(
        {
            "id": "HKO",
            "icaoId": None,
            "iataId": None,
            "faaId": None,
            "wmoId": None,
            "site": "Hong Kong Observatory Headquarters",
            "lat": 22 + 18 / 60 + 7 / 3600,
            "lon": 114 + 10 / 60 + 27 / 3600,
            "elev": 32,
            "state": None,
            "country": "HK",
            "priority": None,
            "siteType": ["OFFICIAL_CLIMATE_STATION"],
            "metadataSource": "https://www.hko.gov.hk/en/cis/stn.htm",
        }
    )
    return rows


def enrich_stations(root: Path) -> dict[str, Any]:
    root = root.resolve()
    raw = root / "raw"
    derived = root / "derived"
    contracts_path = derived / "resolution_contracts.csv"
    markets_path = derived / "markets.jsonl"
    if not contracts_path.exists() or not markets_path.exists():
        raise ClimateResearchError("Ejecute primero capture")
    contracts = read_csv(contracts_path)
    raw_station_path = raw / "aviation_weather_stations.json"
    if raw_station_path.exists():
        station_rows = json.loads(raw_station_path.read_text(encoding="utf-8"))
    else:
        station_rows = fetch_station_catalog([str(row.get("station_id") or "") for row in contracts])
        write_json(raw_station_path, station_rows)
    by_id = {
        str(row.get("icaoId") or row.get("id") or "").upper(): row
        for row in station_rows
        if row.get("icaoId") or row.get("id")
    }
    station_catalog: list[dict[str, Any]] = []
    for station_id in sorted({str(row.get("station_id") or "") for row in contracts if row.get("station_id")}):
        metadata = by_id.get(station_id.upper(), {})
        station_catalog.append(
            {
                "station_id": station_id,
                "icao": metadata.get("icaoId"),
                "iata": metadata.get("iataId"),
                "wmo_id": metadata.get("wmoId"),
                "station_name": metadata.get("site"),
                "latitude": metadata.get("lat"),
                "longitude": metadata.get("lon"),
                "elevation_m": metadata.get("elev"),
                "country": metadata.get("country"),
                "state": metadata.get("state"),
                "site_type": stable_json(metadata.get("siteType") or []),
                "metadata_source": metadata.get("metadataSource") or "https://aviationweather.gov/api/data/stationinfo",
                "metadata_complete": all(metadata.get(key) is not None for key in ("lat", "lon", "elev", "country")),
            }
        )
    write_csv(derived / "station_catalog.csv", station_catalog)
    catalog_by_id = {row["station_id"]: row for row in station_catalog}
    enriched: list[dict[str, Any]] = []
    for row in contracts:
        result: dict[str, Any] = dict(row)
        metadata = catalog_by_id.get(str(row.get("station_id") or ""), {})
        for field in ("icao", "wmo_id", "country", "latitude", "longitude", "elevation_m"):
            result[field] = metadata.get(field) or row.get(field) or None
        if not result.get("station_name"):
            result["station_name"] = metadata.get("station_name")
        result["station_metadata_complete"] = bool(metadata.get("metadata_complete"))
        result["rules_complete"] = bool(
            result.get("city")
            and result.get("market_type")
            and result.get("station_id")
            and result.get("unit")
            and result.get("precision")
        )
        enriched.append(result)
    write_csv(derived / "resolution_contracts_enriched.csv", enriched)
    missing = [row for row in station_catalog if not row["metadata_complete"]]
    summary = {
        "schema": SCHEMA,
        "cut_off": iso(CUT_OFF),
        "unique_station_ids": len(station_catalog),
        "complete_station_metadata": len(station_catalog) - len(missing),
        "missing_station_metadata": [row["station_id"] for row in missing],
        "contracts": len(enriched),
        "complete_contracts": sum(bool(row["rules_complete"]) for row in enriched),
        "source_types": dict(Counter(str(row["source_type"]) for row in enriched)),
        "files": [
            {
                "path": str(path.relative_to(root)),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in (
                raw_station_path,
                derived / "station_catalog.csv",
                derived / "resolution_contracts_enriched.csv",
            )
        ],
    }
    write_json(derived / "station_summary.json", summary)
    return summary


def _request_text(url: str, attempts: int = 4, timeout: int = 120) -> str:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8-sig", "replace")
        except Exception as exc:  # pragma: no cover - depende de red
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2.0 * (attempt + 1))
    raise ClimateResearchError(f"No se pudo consultar {url}: {last}")


def _load_market_context(root: Path) -> tuple[list[dict[str, str]], dict[str, dict[str, str]], list[dict[str, str]]]:
    derived = root / "derived"
    markets = read_csv(derived / "markets.csv")
    contracts = read_csv(derived / "resolution_contracts_enriched.csv")
    stations = read_csv(derived / "station_catalog.csv")
    return markets, {row["slug"]: row for row in contracts}, stations


def _station_date_ranges(
    markets: list[dict[str, str]], contracts: dict[str, dict[str, str]]
) -> dict[str, tuple[str, str]]:
    values: dict[str, list[str]] = defaultdict(list)
    for market in markets:
        contract = contracts.get(market["slug"], {})
        station = str(contract.get("station_id") or "")
        market_date = str(market.get("market_date") or market.get("end_date") or "")[:10]
        if station and market_date:
            values[station].append(market_date)
    return {station: (min(dates), max(dates)) for station, dates in values.items() if dates}


def _download_previous_run_job(
    station: dict[str, str],
    model: str,
    year: int,
    start_date: str,
    end_date: str,
    path: Path,
) -> dict[str, Any]:
    variables = ",".join(f"temperature_2m_previous_day{lead}" for lead in range(1, 8))
    params = {
        "latitude": station["latitude"],
        "longitude": station["longitude"],
        "start_date": start_date,
        "end_date": end_date,
        "hourly": variables,
        "models": model,
        "timezone": "auto",
        "temperature_unit": "celsius",
    }
    url = f"https://previous-runs-api.open-meteo.com/v1/forecast?{urllib.parse.urlencode(params)}"
    payload = request_json(url, timeout=180)
    if not isinstance(payload, dict) or not isinstance(payload.get("hourly"), dict):
        raise ClimateResearchError(f"Forecast histórico inválido: {station['station_id']} {model} {year}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    non_null = sum(
        value is not None
        for key, values in payload["hourly"].items()
        if key != "time" and isinstance(values, list)
        for value in values
    )
    return {
        "kind": "OPEN_METEO_PREVIOUS_RUNS",
        "station": station["station_id"],
        "model": model,
        "year": year,
        "path": str(path),
        "bytes": path.stat().st_size,
        "non_null": non_null,
        "status": "OK",
    }


def _iem_network_and_station(station: dict[str, str]) -> tuple[str, str]:
    icao = str(station.get("icao") or station.get("station_id") or "").upper()
    country = str(station.get("country") or "").upper()
    state = str(station.get("state") or "").upper()
    if country == "US" and len(icao) == 4 and icao.startswith("K"):
        return f"{state}_ASOS", icao[1:]
    return f"{country}__ASOS", icao


def _download_iem_job(station: dict[str, str], start_date: str, end_date: str, path: Path) -> dict[str, Any]:
    network, iem_station = _iem_network_and_station(station)
    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    params: list[tuple[str, str]] = [
        ("network", network),
        ("station", iem_station),
        ("data", "tmpf"),
        ("data", "dwpf"),
        ("data", "relh"),
        ("data", "drct"),
        ("data", "sknt"),
        ("data", "mslp"),
        ("data", "p01i"),
        ("data", "skyc1"),
        ("year1", str(start.year)),
        ("month1", str(start.month)),
        ("day1", str(start.day)),
        ("year2", str(end.year)),
        ("month2", str(end.month)),
        ("day2", str(end.day)),
        ("tz", "Etc/UTC"),
        ("format", "onlycomma"),
        ("latlon", "yes"),
        ("elev", "yes"),
        ("missing", "M"),
        ("trace", "T"),
        ("direct", "no"),
        ("report_type", "3"),
        ("report_type", "4"),
    ]
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(params)
    text = _request_text(url, timeout=180)
    if not text.startswith("station,valid"):
        raise ClimateResearchError(f"IEM no devolvió CSV para {station['station_id']}: {text[:200]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return {
        "kind": "IEM_ASOS_METAR",
        "station": station["station_id"],
        "network": network,
        "path": str(path),
        "bytes": path.stat().st_size,
        "rows": max(0, text.count("\n") - 1),
        "status": "OK",
    }


def _download_hko_job(kind: str, year: int, path: Path) -> dict[str, Any]:
    params = {"dataType": kind, "year": year, "rformat": "json", "station": "HKO"}
    url = "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php?" + urllib.parse.urlencode(params)
    payload = request_json(url, timeout=90)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ClimateResearchError(f"HKO inválido: {kind} {year}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    return {
        "kind": "HKO_OFFICIAL_DAILY",
        "variable": kind,
        "year": year,
        "path": str(path),
        "bytes": path.stat().st_size,
        "rows": len(payload["data"]),
        "status": "OK",
    }


def download_weather_raw(root: Path, workers: int = 8) -> dict[str, Any]:
    root = root.resolve()
    markets, contracts, stations = _load_market_context(root)
    ranges = _station_date_ranges(markets, contracts)
    station_by_id = {row["station_id"]: row for row in stations}
    raw_weather = root / "raw" / "weather"
    jobs: list[tuple[str, tuple[Any, ...], Path]] = []
    skipped: list[dict[str, Any]] = []
    for station_id, (start_date, end_date) in sorted(ranges.items()):
        station = station_by_id.get(station_id)
        if not station:
            continue
        start_year, end_year = int(start_date[:4]), int(end_date[:4])
        for year in range(start_year, end_year + 1):
            year_start = max(start_date, f"{year}-01-01")
            year_end = min(end_date, f"{year}-12-31")
            for model in OPEN_METEO_MODELS:
                path = raw_weather / "open_meteo_previous_runs" / f"{station_id}__{model}__{year}.json.gz"
                if path.exists():
                    skipped.append({"kind": "OPEN_METEO_PREVIOUS_RUNS", "path": str(path), "status": "CACHED"})
                else:
                    jobs.append(("forecast", (station, model, year, year_start, year_end, path), path))
        if station_id != "HKO":
            path = raw_weather / "iem_asos" / f"{station_id}.csv.gz"
            if path.exists():
                skipped.append({"kind": "IEM_ASOS_METAR", "path": str(path), "status": "CACHED"})
            else:
                jobs.append(("iem", (station, start_date, end_date, path), path))
    hko_range = ranges.get("HKO")
    if hko_range:
        for year in range(int(hko_range[0][:4]), int(hko_range[1][:4]) + 1):
            for kind in ("CLMMAXT", "CLMMINT"):
                path = raw_weather / "hko" / f"{kind}__{year}.json.gz"
                if path.exists():
                    skipped.append({"kind": "HKO_OFFICIAL_DAILY", "path": str(path), "status": "CACHED"})
                else:
                    jobs.append(("hko", (kind, year, path), path))

    results: list[dict[str, Any]] = list(skipped)
    failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as pool:
        futures = {}
        for kind, arguments, path in jobs:
            function = _download_previous_run_job if kind == "forecast" else _download_iem_job if kind == "iem" else _download_hko_job
            futures[pool.submit(function, *arguments)] = (kind, path)
        for index, future in enumerate(as_completed(futures), start=1):
            kind, path = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # pragma: no cover - depende de red
                failures.append({"kind": kind, "path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            if index % 50 == 0 or index == len(futures):
                print(stable_json({"weather_download_progress": index, "total": len(futures), "failures": len(failures)}), flush=True)
    manifest = {
        "schema": SCHEMA,
        "cut_off": iso(CUT_OFF),
        "captured_at": iso(utc_now()),
        "models": list(OPEN_METEO_MODELS),
        "station_ranges": ranges,
        "jobs": len(jobs),
        "cached": len(skipped),
        "completed": len(results) - len(skipped),
        "failures": failures,
        "results": results,
    }
    write_json(root / "raw" / "weather_download_manifest.json", manifest)
    return manifest


WEATHER_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE station_metadata(
 station_id TEXT PRIMARY KEY,icao TEXT,iata TEXT,wmo_id TEXT,station_name TEXT,
 latitude REAL,longitude REAL,elevation_m REAL,country TEXT,state TEXT,timezone TEXT
);
CREATE TABLE forecasts_daily(
 station_id TEXT NOT NULL,date TEXT NOT NULL,model TEXT NOT NULL,lead_days INTEGER NOT NULL,
 forecast_max_c REAL,forecast_min_c REAL,timezone TEXT,grid_elevation_m REAL,
 PRIMARY KEY(station_id,date,model,lead_days)
);
CREATE TABLE observations_hourly(
 station_id TEXT NOT NULL,valid_utc TEXT NOT NULL,valid_local TEXT NOT NULL,local_date TEXT NOT NULL,
 temp_c REAL,dewpoint_c REAL,relative_humidity REAL,wind_direction REAL,wind_knots REAL,
 sea_level_pressure REAL,precip_in REAL,cloud_code TEXT,source TEXT NOT NULL,
 PRIMARY KEY(station_id,valid_utc)
);
CREATE TABLE observations_daily(
 station_id TEXT NOT NULL,date TEXT NOT NULL,max_c REAL,min_c REAL,max_time_local TEXT,min_time_local TEXT,
 observation_count INTEGER NOT NULL,observed_hour_count INTEGER NOT NULL,source TEXT NOT NULL,completeness TEXT,
 PRIMARY KEY(station_id,date)
);
CREATE TABLE event_truth(
 slug TEXT PRIMARY KEY,event_id TEXT,station_id TEXT,date TEXT,market_type TEXT,unit TEXT,precision REAL,
 winner_bucket TEXT,winner_lower REAL,winner_upper REAL,observed_value REAL,observed_value_c REAL,
 observation_source TEXT,reconstruction_matches INTEGER,split TEXT
);
CREATE INDEX idx_forecasts_station_date ON forecasts_daily(station_id,date);
CREATE INDEX idx_obs_daily_station_date ON observations_daily(station_id,date);
CREATE INDEX idx_truth_split ON event_truth(split,market_type,station_id);
"""


def _float_or_none(value: Any) -> float | None:
    if value in (None, "", "M", "T", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _f_to_c(value: float | None) -> float | None:
    return None if value is None else (value - 32.0) * 5.0 / 9.0


def _round_resolution(value: float, unit: str, precision: float) -> float:
    if precision <= 0:
        return value
    decimals = max(0, int(round(-math.log10(precision)))) if precision < 1 else 0
    return round(value / precision) * precision if decimals == 0 else round(value, decimals)


def _parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def build_weather_database(root: Path) -> dict[str, Any]:
    root = root.resolve()
    derived = root / "derived"
    raw_weather = root / "raw" / "weather"
    final_db = derived / "climate_weather.db"
    summary_path = derived / "weather_database_summary.json"
    if final_db.exists() and summary_path.exists():
        return json.loads(summary_path.read_text(encoding="utf-8"))
    partial = final_db.with_suffix(".db.partial")
    for stale in (partial, Path(f"{partial}-wal"), Path(f"{partial}-shm")):
        if stale.exists():
            stale.unlink()
    database = sqlite3.connect(partial)
    database.row_factory = sqlite3.Row
    database.executescript(WEATHER_DDL)
    markets, contracts, stations = _load_market_context(root)
    station_by_id = {row["station_id"]: row for row in stations}

    timezone_by_station: dict[str, str] = {"HKO": "Asia/Hong_Kong"}
    forecast_rows = 0
    forecast_non_null = 0
    for path in sorted((raw_weather / "open_meteo_previous_runs").glob("*.json.gz")):
        station_id, model, _ = path.name.removesuffix(".json.gz").split("__")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        timezone_name = str(payload.get("timezone") or "UTC")
        timezone_by_station.setdefault(station_id, timezone_name)
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        dates = [str(value)[:10] for value in times]
        rows: list[tuple[Any, ...]] = []
        for lead in range(1, 8):
            values = hourly.get(f"temperature_2m_previous_day{lead}") or []
            grouped: dict[str, list[float]] = defaultdict(list)
            for date, value in zip(dates, values):
                numeric = _float_or_none(value)
                if numeric is not None:
                    grouped[date].append(numeric)
            for date in sorted(set(dates)):
                day_values = grouped.get(date, [])
                rows.append(
                    (
                        station_id,
                        date,
                        model,
                        lead,
                        max(day_values) if day_values else None,
                        min(day_values) if day_values else None,
                        timezone_name,
                        _float_or_none(payload.get("elevation")),
                    )
                )
                forecast_non_null += int(bool(day_values))
        database.executemany(
            "INSERT OR REPLACE INTO forecasts_daily VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )
        forecast_rows += len(rows)
        database.commit()

    for row in stations:
        timezone_name = timezone_by_station.get(row["station_id"], "UTC")
        database.execute(
            "INSERT INTO station_metadata VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["station_id"],
                row.get("icao") or None,
                row.get("iata") or None,
                row.get("wmo_id") or None,
                row.get("station_name") or None,
                _float_or_none(row.get("latitude")),
                _float_or_none(row.get("longitude")),
                _float_or_none(row.get("elevation_m")),
                row.get("country") or None,
                row.get("state") or None,
                timezone_name,
            ),
        )
    database.commit()

    observation_rows = 0
    for path in sorted((raw_weather / "iem_asos").glob("*.csv.gz")):
        station_id = path.name.removesuffix(".csv.gz")
        timezone_name = timezone_by_station.get(station_id, "UTC")
        zone = ZoneInfo(timezone_name)
        batch: list[tuple[Any, ...]] = []
        station_daily: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                temp_c = _f_to_c(_float_or_none(row.get("tmpf")))
                if temp_c is None:
                    continue
                valid_utc = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                valid_local = valid_utc.astimezone(zone)
                station_daily[valid_local.date().isoformat()].append((valid_local, temp_c))
                batch.append(
                    (
                        station_id,
                        iso(valid_utc),
                        valid_local.isoformat(),
                        valid_local.date().isoformat(),
                        temp_c,
                        _f_to_c(_float_or_none(row.get("dwpf"))),
                        _float_or_none(row.get("relh")),
                        _float_or_none(row.get("drct")),
                        _float_or_none(row.get("sknt")),
                        _float_or_none(row.get("mslp")),
                        _float_or_none(row.get("p01i")),
                        row.get("skyc1") or None,
                        "IEM_ASOS_METAR",
                    )
                )
                if len(batch) >= 5000:
                    database.executemany("INSERT OR REPLACE INTO observations_hourly VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                    observation_rows += len(batch)
                    batch.clear()
        if batch:
            database.executemany("INSERT OR REPLACE INTO observations_hourly VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            observation_rows += len(batch)
        for date, observations in station_daily.items():
            max_time, max_value = max(observations, key=lambda item: (item[1], -item[0].timestamp()))
            min_time, min_value = min(observations, key=lambda item: (item[1], item[0].timestamp()))
            database.execute(
                "INSERT OR REPLACE INTO observations_daily VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    station_id,
                    date,
                    max_value,
                    min_value,
                    max_time.isoformat(),
                    min_time.isoformat(),
                    len(observations),
                    len({value[0].hour for value in observations}),
                    "IEM_ASOS_METAR",
                    "OBSERVED_UNFILLED",
                ),
            )
        database.commit()

    hko_daily: dict[str, dict[str, Any]] = defaultdict(dict)
    for path in sorted((raw_weather / "hko").glob("*.json.gz")):
        kind, _ = path.name.removesuffix(".json.gz").split("__")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        for row in payload.get("data") or []:
            if len(row) < 4:
                continue
            date = f"{int(row[0]):04d}-{int(row[1]):02d}-{int(row[2]):02d}"
            hko_daily[date][kind] = _float_or_none(row[3])
            hko_daily[date][f"{kind}_completeness"] = row[4] if len(row) > 4 else None
    for date, values in hko_daily.items():
        database.execute(
            "INSERT OR REPLACE INTO observations_daily VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "HKO",
                date,
                values.get("CLMMAXT"),
                values.get("CLMMINT"),
                None,
                None,
                0,
                0,
                "HKO_OFFICIAL_DAILY",
                stable_json({"max": values.get("CLMMAXT_completeness"), "min": values.get("CLMMINT_completeness")}),
            ),
        )
    database.commit()

    reconciliations: list[dict[str, Any]] = []
    for market in markets:
        winner_label = str(market.get("winner_bucket") or "")
        contract = contracts.get(market["slug"], {})
        station_id = str(contract.get("station_id") or "")
        date = str(market.get("market_date") or market.get("end_date") or "")[:10]
        if not winner_label or not station_id or not date:
            continue
        try:
            lower, upper, unit = parse_bucket(winner_label)
        except ClimateResearchError:
            continue
        observed = database.execute(
            "SELECT * FROM observations_daily WHERE station_id=? AND date=?",
            (station_id, date),
        ).fetchone()
        value_c = None
        source = None
        if observed:
            value_c = observed["max_c"] if market["market_type"] == "HIGHEST" else observed["min_c"]
            source = observed["source"]
        value = None
        matched = None
        precision = _float_or_none(contract.get("precision")) or 1.0
        if value_c is not None:
            converted = value_c * 9.0 / 5.0 + 32.0 if unit == "°F" else value_c
            value = _round_resolution(converted, unit, precision)
            matched = int((lower is None or value >= lower) and (upper is None or value <= upper))
        database.execute(
            "INSERT OR REPLACE INTO event_truth VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                market["slug"],
                market["event_id"],
                station_id,
                date,
                market["market_type"],
                unit,
                precision,
                winner_label,
                lower,
                upper,
                value,
                value_c,
                source,
                matched,
                market["split"],
            ),
        )
        reconciliations.append(
            {
                "slug": market["slug"],
                "station_id": station_id,
                "date": date,
                "market_type": market["market_type"],
                "unit": unit,
                "winner_bucket": winner_label,
                "observed_value": value,
                "observation_source": source,
                "matches": matched,
                "split": market["split"],
            }
        )
    database.commit()
    database.execute("PRAGMA optimize")
    daily_rows = database.execute("SELECT COUNT(*) FROM observations_daily").fetchone()[0]
    event_truth_rows = database.execute("SELECT COUNT(*) FROM event_truth").fetchone()[0]
    matched_rows = database.execute("SELECT COUNT(*) FROM event_truth WHERE reconstruction_matches=1").fetchone()[0]
    comparable_rows = database.execute("SELECT COUNT(*) FROM event_truth WHERE reconstruction_matches IS NOT NULL").fetchone()[0]
    database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.execute("PRAGMA journal_mode=DELETE")
    database.close()
    os.replace(partial, final_db)
    write_csv(derived / "resolution_reconciliation.csv", reconciliations)
    by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in reconciliations:
        if row["matches"] is not None:
            by_station[row["station_id"]].append(row)
    station_reconciliation = [
        {
            "station_id": station,
            "comparable_events": len(rows),
            "matches": sum(int(row["matches"]) for row in rows),
            "match_rate": sum(int(row["matches"]) for row in rows) / len(rows),
        }
        for station, rows in by_station.items()
    ]
    write_csv(
        derived / "station_resolution_reconciliation.csv",
        sorted(station_reconciliation, key=lambda row: (-row["match_rate"], -row["comparable_events"], row["station_id"])),
    )
    summary = {
        "schema": SCHEMA,
        "cut_off": iso(CUT_OFF),
        "models": list(OPEN_METEO_MODELS),
        "stations": len(stations),
        "forecast_rows": forecast_rows,
        "forecast_non_null_rows": forecast_non_null,
        "forecast_coverage": forecast_non_null / max(1, forecast_rows),
        "observation_rows": observation_rows,
        "observation_daily_rows": daily_rows,
        "event_truth_rows": event_truth_rows,
        "comparable_resolution_events": comparable_rows,
        "matching_resolution_events": matched_rows,
        "resolution_match_rate": matched_rows / max(1, comparable_rows),
        "database": str(final_db),
        "database_bytes": final_db.stat().st_size,
        "database_sha256": sha256(final_db),
    }
    write_json(summary_path, summary)
    return summary


def weather_pipeline(root: Path, workers: int = 8) -> dict[str, Any]:
    manifest = download_weather_raw(root, workers=workers)
    database = build_weather_database(root)
    return {
        "download": {
            "jobs": manifest["jobs"],
            "cached": manifest["cached"],
            "completed": manifest["completed"],
            "failures": len(manifest["failures"]),
        },
        "database": database,
    }


def capture(root: Path) -> dict[str, Any]:
    root = root.resolve()
    raw = root / "raw"
    derived = root / "derived"
    raw_events = raw / "gamma_daily_temperature_events.json"
    if raw_events.exists():
        events = json.loads(raw_events.read_text(encoding="utf-8"))
        pages: list[dict[str, Any]] = []
        captured_at = None
    else:
        events, pages = fetch_daily_temperature_events()
        captured_at = iso(utc_now())
        write_json(raw_events, events)
        write_json(raw / "gamma_capture_pages.json", pages)
    filtered, contracts, markets = normalize_events(events)
    summaries = city_summary(contracts, markets)
    write_jsonl(derived / "events.jsonl", filtered)
    write_csv(derived / "resolution_contracts.csv", contracts)
    write_jsonl(derived / "markets.jsonl", markets)
    write_csv(derived / "markets.csv", markets)
    write_csv(derived / "city_market_inventory.csv", summaries)
    output_files = [
        path
        for path in [
            raw_events,
            raw / "gamma_capture_pages.json",
            derived / "events.jsonl",
            derived / "resolution_contracts.csv",
            derived / "markets.jsonl",
            derived / "markets.csv",
            derived / "city_market_inventory.csv",
        ]
        if path.exists()
    ]
    summary = {
        "schema": SCHEMA,
        "cut_off": iso(CUT_OFF),
        "captured_at": captured_at,
        "gamma_tag_events": len(events),
        "temperature_events": len(markets),
        "resolved_events": sum(bool(row["winner_bucket"]) for row in markets),
        "shadow_forward_events": sum(row["split"] == "SHADOW_FORWARD" for row in markets),
        "cities": len({row["city"] for row in markets}),
        "city_type_families": len(summaries),
        "complete_resolution_contracts": sum(bool(row["rules_complete"]) for row in contracts),
        "source_types": dict(Counter(str(row["source_type"]) for row in contracts)),
        "market_types": dict(Counter(str(row["market_type"]) for row in markets)),
        "splits": dict(Counter(str(row["split"]) for row in markets)),
        "files": [
            {"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in output_files
        ],
    }
    write_json(derived / "capture_summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Investigación auditada de mercados diarios de temperatura")
    parser.add_argument("command", choices=("capture", "stations", "weather"))
    parser.add_argument("--root", default="data/climate_research_v001")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    if args.command == "capture":
        result = capture(Path(args.root))
    elif args.command == "stations":
        result = enrich_stations(Path(args.root))
    elif args.command == "weather":
        result = weather_pipeline(Path(args.root), workers=args.workers)
    else:  # pragma: no cover
        raise ClimateResearchError(f"Comando no soportado: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

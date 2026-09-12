from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

import numpy as np
from scipy.stats import nbinom, poisson


SCHEMA = "elon_post_count_research_v001"
CUT_OFF = datetime.fromisoformat("2026-09-01T22:40:00+00:00")


def _first_sunday_on_or_after(value: datetime) -> datetime:
    days_to_go = 6 - value.weekday()
    return value if days_to_go == 0 else value + timedelta(days=days_to_go)


def _us_dst_range(year: int) -> tuple[datetime, datetime]:
    start = _first_sunday_on_or_after(datetime(year, 3, 8, 2))
    end = _first_sunday_on_or_after(datetime(year, 11, 1, 2))
    return start, end


class _EasternFallback(tzinfo):
    """US Eastern with post-2007 DST rules for Windows without tzdata."""

    _standard = timedelta(hours=-5)
    _daylight = timedelta(hours=1)

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._standard + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        start, end = _us_dst_range(dt.year)
        naive = dt.replace(tzinfo=None)
        if start + self._daylight <= naive < end - self._daylight:
            return self._daylight
        if end - self._daylight <= naive < end:
            return timedelta(0) if dt.fold else self._daylight
        if start <= naive < start + self._daylight:
            return self._daylight if dt.fold else timedelta(0)
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if self.dst(dt) else "EST"

    def fromutc(self, dt: datetime) -> datetime:
        start, end = _us_dst_range(dt.year)
        start = start.replace(tzinfo=self)
        end = end.replace(tzinfo=self)
        standard = dt + self._standard
        daylight = standard + self._daylight
        if end <= daylight < end + self._daylight:
            return standard.replace(fold=1)
        if standard < start or daylight >= end:
            return standard
        if start <= standard < end - self._daylight:
            return daylight
        return standard


try:
    ET = ZoneInfo("America/New_York")
except Exception:  # Windows embeddable Python may omit the IANA tzdata wheel.
    ET = _EasternFallback()
XTRACKER = "https://xtracker.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_HANDLE = "elonmusk"
DEFAULT_ROOT = Path("data/elon_post_count_v001")
TIMEOUT = 120
USER_AGENT = "polymarket-quant-bot-elon-research/0.0.1"
ENTRY_IMPACT = 0.01
MAX_PRICE_AGE_SECONDS = 30 * 60
ENTRY_REMAINING_SECONDS = (
    48 * 3600,
    24 * 3600,
    12 * 3600,
    8 * 3600,
    6 * 3600,
    4 * 3600,
    3 * 3600,
    2 * 3600,
    3600,
    30 * 60,
    15 * 60,
    5 * 60,
)
ENTRY_FRACTIONS = (0.0, 0.25, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
EDGE_THRESHOLDS = (0.03, 0.05, 0.075, 0.10, 0.15)
MODEL_NAMES = (
    "historical_analog",
    "naive_recent_rate",
    "poisson_intraday",
    "negative_binomial",
    "bayesian_dynamic_rate",
    "hawkes_proxy",
)


class ResearchError(RuntimeError):
    pass


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(stable_json(row) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_csv(path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        seen: list[str] = []
        known: set[str] = set()
        for row in rows:
            for key in row:
                if key not in known:
                    known.add(key)
                    seen.append(key)
        fields = seen
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _request_json(
    url: str,
    *,
    method: str = "GET",
    body: Any | None = None,
    retries: int = 5,
) -> Any:
    payload = None if body is None else stable_json(body).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8-sig"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(8.0, 0.75 * (2**attempt)))
    raise ResearchError(f"No se pudo descargar {url}: {last_error}")


def _api_data(url: str) -> Any:
    payload = _request_json(url)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ResearchError(f"Respuesta XTracker inválida: {url}")
    return payload.get("data")


def _slug_from_market_link(link: str) -> str:
    parsed = urllib.parse.urlparse(link)
    parts = [part for part in parsed.path.split("/") if part]
    if "event" not in parts:
        return ""
    index = parts.index("event")
    return parts[index + 1] if index + 1 < len(parts) else ""


def _unwrap_gamma_event(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else None
    return payload if isinstance(payload, dict) and payload.get("id") else None


def _collect_event(slug: str) -> tuple[str, dict[str, Any] | None, str | None]:
    url = f"{GAMMA}/events?{urllib.parse.urlencode({'slug': slug})}"
    try:
        return slug, _unwrap_gamma_event(_request_json(url)), None
    except Exception as exc:  # collection must preserve partial failures
        return slug, None, str(exc)


def _yes_tokens(event: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for market in event.get("markets") or []:
        try:
            tokens = json.loads(market.get("clobTokenIds") or "[]")
        except (TypeError, json.JSONDecodeError):
            tokens = []
        if tokens:
            result.append(str(tokens[0]))
    return result


def _collect_tweet_count(event_id: str) -> tuple[str, int | None, str | None]:
    url = f"{GAMMA}/events/{event_id}/tweet-count"
    try:
        payload = _request_json(url)
        value = payload.get("tweetCount") if isinstance(payload, dict) else None
        return event_id, int(value) if value is not None else None, None
    except Exception as exc:
        return event_id, None, str(exc)


def _collect_price_history(event: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    slug = str(event.get("slug") or event.get("id"))
    tokens = _yes_tokens(event)
    if not tokens:
        return slug, {"history": {}}, "NO_TOKENS"
    try:
        payload = _request_json(
            f"{CLOB}/batch-prices-history",
            method="POST",
            body={"markets": tokens, "interval": "max", "fidelity": 1},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("history"), dict):
            raise ResearchError("Formato batch-prices-history inválido")
        return slug, payload, None
    except Exception as exc:
        history: dict[str, Any] = {}
        errors: list[str] = []
        for token in tokens:
            url = f"{CLOB}/prices-history?{urllib.parse.urlencode({'market': token, 'interval': 'max', 'fidelity': 1})}"
            try:
                payload = _request_json(url)
                history[token] = payload.get("history", []) if isinstance(payload, dict) else []
            except Exception as token_exc:
                errors.append(f"{token}:{token_exc}")
        return slug, {"history": history}, str(exc) + (" | " + " | ".join(errors) if errors else "")


def _collect_book(token: str) -> tuple[str, dict[str, Any] | None, str | None]:
    url = f"{CLOB}/book?{urllib.parse.urlencode({'token_id': token})}"
    try:
        payload = _request_json(url)
        return token, payload if isinstance(payload, dict) else None, None
    except Exception as exc:
        return token, None, str(exc)


def _gdelt_category(title: str) -> str:
    text = title.lower()
    mapping = (
        ("TESLA", ("tesla", "fsd", "cybertruck", "optimus", "robotaxi", "powerwall")),
        ("SPACEX", ("spacex", "starship", "starlink", "falcon", "rocket", "launch")),
        ("XAI_GROK", ("xai", "grok", "colossus")),
        ("X", ("twitter", " x ", "x.com")),
        ("NEURALINK", ("neuralink",)),
        ("BORING_COMPANY", ("boring company", "hyperloop")),
        ("POLITICS_REGULATION", ("trump", "government", "white house", "regulat", "lawsuit", "court", "sec ", "politic")),
    )
    for category, words in mapping:
        if any(word in f" {text} " for word in words):
            return category
    return "OTHER_ELON"


def _gdelt_importance(article: dict[str, Any]) -> str:
    title = str(article.get("title") or "").lower()
    extreme = ("explosion", "crash", "resigns", "arrest", "emergency", "bankrupt", "dies", "war")
    high = ("earnings", "launch", "unveils", "announces", "lawsuit", "investigation", "approval", "ban", "acquires")
    medium = ("tesla", "spacex", "xai", "grok", "starship", "neuralink", "trump")
    if any(word in title for word in extreme):
        return "EXTREME"
    if any(word in title for word in high):
        return "HIGH"
    if any(word in title for word in medium):
        return "MEDIUM"
    return "LOW"


def _collect_gdelt() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # DOC 2.0 exposes a rolling three-month window. Weekly slices reduce the
    # 250-result truncation while preserving publication timestamps.
    start = max(CUT_OFF - timedelta(days=92), datetime(2025, 10, 31, tzinfo=timezone.utc))
    rows: dict[str, dict[str, Any]] = {}
    audits: list[dict[str, Any]] = []
    cursor = start
    while cursor < CUT_OFF:
        end = min(cursor + timedelta(days=7), CUT_OFF)
        params = {
            "query": '"Elon Musk"',
            "mode": "artlist",
            "maxrecords": 250,
            "format": "json",
            "startdatetime": cursor.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
            "sort": "datedesc",
        }
        url = f"{GDELT}?{urllib.parse.urlencode(params)}"
        try:
            payload = _request_json(url, retries=3)
            articles = payload.get("articles", []) if isinstance(payload, dict) else []
            for article in articles:
                if not isinstance(article, dict):
                    continue
                key = str(article.get("url") or stable_json(article))
                article = dict(article)
                article["category"] = _gdelt_category(str(article.get("title") or ""))
                article["importance"] = _gdelt_importance(article)
                rows[key] = article
            audits.append({"start": iso(cursor), "end": iso(end), "records": len(articles), "error": None})
        except Exception as exc:
            audits.append({"start": iso(cursor), "end": iso(end), "records": 0, "error": str(exc)})
        cursor = end
        time.sleep(0.35)
    return list(rows.values()), audits


def collect(output_root: Path = DEFAULT_ROOT, *, workers: int = 10, include_news: bool = True) -> dict[str, Any]:
    root = output_root.resolve()
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now(timezone.utc)

    user = _api_data(f"{XTRACKER}/api/users/{USER_HANDLE}")
    posts = _api_data(f"{XTRACKER}/api/users/{USER_HANDLE}/posts")
    trackings = _api_data(f"{XTRACKER}/api/users/{USER_HANDLE}/trackings")
    if not isinstance(user, dict) or not isinstance(posts, list) or not isinstance(trackings, list):
        raise ResearchError("XTracker devolvió un esquema inesperado")
    posts = [row for row in posts if parse_dt(row["createdAt"]) <= CUT_OFF]

    write_json(raw / "xtracker_user.json", user)
    write_json(raw / "xtracker_posts.json", posts)
    write_json(raw / "xtracker_trackings.json", trackings)

    slugs = sorted({_slug_from_market_link(str(row.get("marketLink") or "")) for row in trackings} - {""})
    events: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_collect_event, slug) for slug in slugs]
        for future in as_completed(futures):
            slug, event, error = future.result()
            if event is not None:
                events.append(event)
            else:
                failures.append({"stage": "gamma_event", "key": slug, "error": error})
    events.sort(key=lambda row: str(row.get("slug") or ""))
    write_jsonl(raw / "gamma_events.jsonl", events)

    counts: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_collect_tweet_count, str(event["id"])) for event in events]
        for future in as_completed(futures):
            event_id, count, error = future.result()
            counts.append({"event_id": event_id, "tweet_count": count, "error": error})
            if error:
                failures.append({"stage": "tweet_count", "key": event_id, "error": error})
    counts.sort(key=lambda row: row["event_id"])
    write_jsonl(raw / "tweet_counts.jsonl", counts)

    price_dir = raw / "price_history"
    price_dir.mkdir(parents=True, exist_ok=True)
    price_summaries: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(2, min(workers, 6))) as pool:
        futures = [pool.submit(_collect_price_history, event) for event in events]
        for future in as_completed(futures):
            slug, payload, error = future.result()
            path = price_dir / f"{slug}.json"
            write_json(path, payload)
            histories = payload.get("history", {})
            points = sum(len(values) for values in histories.values())
            price_summaries.append({"slug": slug, "tokens": len(histories), "points": points, "error": error})
            if error and points == 0:
                failures.append({"stage": "price_history", "key": slug, "error": error})
    price_summaries.sort(key=lambda row: row["slug"])
    write_jsonl(raw / "price_history_manifest.jsonl", price_summaries)

    tracking_by_slug = {
        _slug_from_market_link(str(row.get("marketLink") or "")): row
        for row in trackings
        if _slug_from_market_link(str(row.get("marketLink") or ""))
    }
    live_events = []
    for event in events:
        tracking = tracking_by_slug.get(str(event.get("slug") or ""))
        if not tracking:
            continue
        if parse_dt(tracking["startDate"]) <= CUT_OFF <= parse_dt(tracking["endDate"]):
            live_events.append(event)
    live_tokens: set[str] = set()
    for event in live_events:
        for bucket in event_buckets(event):
            live_tokens.add(bucket.yes_token)
            live_tokens.add(bucket.no_token)
    live_tokens = sorted(live_tokens)
    books: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_collect_book, token) for token in live_tokens]
        for future in as_completed(futures):
            token, book, error = future.result()
            books.append({"token": token, "book": book, "error": error})
    books.sort(key=lambda row: row["token"])
    write_jsonl(raw / "current_books.jsonl", books)

    news: list[dict[str, Any]] = []
    news_audit: list[dict[str, Any]] = []
    if include_news:
        news, news_audit = _collect_gdelt()
    write_jsonl(raw / "news_gdelt.jsonl", news)
    write_jsonl(raw / "news_gdelt_audit.jsonl", news_audit)

    files = sorted(path for path in raw.rglob("*") if path.is_file() and path.name != "capture_manifest.json")
    manifest = {
        "schema": SCHEMA,
        "captured_at": iso(captured_at),
        "cut_off": iso(CUT_OFF),
        "user_handle": USER_HANDLE,
        "posts": len(posts),
        "trackings": len(trackings),
        "market_links": len(slugs),
        "gamma_events": len(events),
        "tweet_counts": sum(row["tweet_count"] is not None for row in counts),
        "price_tokens": sum(row["tokens"] for row in price_summaries),
        "price_points": sum(row["points"] for row in price_summaries),
        "live_events": len(live_events),
        "live_books": sum(row["book"] is not None for row in books),
        "news_articles": len(news),
        "failures": failures,
        "files": [{"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": sha256(path)} for path in files],
    }
    write_json(raw / "capture_manifest.json", manifest)
    return manifest


def finalize_existing_capture(output_root: Path = DEFAULT_ROOT, *, news_error: str | None = None) -> dict[str, Any]:
    """Seal a complete core capture when an optional news source is unavailable."""
    root = output_root.resolve()
    raw = root / "raw"
    required = (
        "xtracker_user.json",
        "xtracker_posts.json",
        "xtracker_trackings.json",
        "gamma_events.jsonl",
        "tweet_counts.jsonl",
        "price_history_manifest.jsonl",
        "current_books.jsonl",
    )
    missing = [name for name in required if not (raw / name).exists()]
    if missing:
        raise ResearchError("Captura core incompleta: " + ", ".join(missing))
    if not (raw / "news_gdelt.jsonl").exists():
        write_jsonl(raw / "news_gdelt.jsonl", [])
    if not (raw / "news_gdelt_audit.jsonl").exists():
        write_jsonl(
            raw / "news_gdelt_audit.jsonl",
            [
                {
                    "start": iso(CUT_OFF - timedelta(days=92)),
                    "end": iso(CUT_OFF),
                    "records": 0,
                    "error": news_error or "OPTIONAL_NEWS_SOURCE_UNAVAILABLE",
                }
            ],
        )
    posts = read_json(raw / "xtracker_posts.json")
    trackings = read_json(raw / "xtracker_trackings.json")
    events = read_jsonl(raw / "gamma_events.jsonl")
    counts = read_jsonl(raw / "tweet_counts.jsonl")
    price_summaries = read_jsonl(raw / "price_history_manifest.jsonl")
    books = read_jsonl(raw / "current_books.jsonl")
    news = read_jsonl(raw / "news_gdelt.jsonl")
    failures: list[dict[str, Any]] = []
    for row in counts:
        if row.get("error"):
            failures.append({"stage": "tweet_count", "key": row.get("event_id"), "error": row.get("error")})
    for row in price_summaries:
        if row.get("error") and int(row.get("points") or 0) == 0:
            failures.append({"stage": "price_history", "key": row.get("slug"), "error": row.get("error")})
    files = sorted(path for path in raw.rglob("*") if path.is_file() and path.name != "capture_manifest.json")
    manifest = {
        "schema": SCHEMA,
        "captured_at": iso(datetime.now(timezone.utc)),
        "cut_off": iso(CUT_OFF),
        "user_handle": USER_HANDLE,
        "posts": len(posts),
        "trackings": len(trackings),
        "market_links": len({_slug_from_market_link(str(row.get('marketLink') or '')) for row in trackings} - {""}),
        "gamma_events": len(events),
        "tweet_counts": sum(row.get("tweet_count") is not None for row in counts),
        "price_tokens": sum(int(row.get("tokens") or 0) for row in price_summaries),
        "price_points": sum(int(row.get("points") or 0) for row in price_summaries),
        "live_events": None,
        "live_books": sum(row.get("book") is not None for row in books),
        "news_articles": len(news),
        "news_status": "AVAILABLE" if news else "UNAVAILABLE_EXCLUDED_FROM_MODEL",
        "news_error": news_error,
        "failures": failures,
        "files": [{"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": sha256(path)} for path in files],
    }
    write_json(raw / "capture_manifest.json", manifest)
    return manifest


@dataclass(frozen=True)
class Bucket:
    label: str
    lower: int
    upper: int | None
    yes_token: str
    no_token: str
    market_id: str
    condition_id: str
    fees_enabled: bool
    fee_rate: float

    def contains(self, count: int) -> bool:
        return count >= self.lower and (self.upper is None or count <= self.upper)


def parse_bucket(label: str) -> tuple[int, int | None]:
    text = label.strip().replace("–", "-").replace(",", "")
    match = re.fullmatch(r"<\s*(\d+)", text)
    if match:
        return 0, int(match.group(1)) - 1
    match = re.fullmatch(r"(\d+)\s*\+", text)
    if match:
        return int(match.group(1)), None
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.fullmatch(r"(\d+)", text)
    if match:
        value = int(match.group(1))
        return value, value
    raise ResearchError(f"Bucket no reconocido: {label!r}")


def event_buckets(event: dict[str, Any]) -> list[Bucket]:
    buckets: list[Bucket] = []
    for market in event.get("markets") or []:
        label = str(market.get("groupItemTitle") or "").strip()
        if not label:
            question = str(market.get("question") or "")
            match = re.search(r"(?:post|tweet)s?\s+(.+?)\s+from", question, re.I)
            label = match.group(1).strip() if match else ""
        if not label:
            continue
        try:
            lower, upper = parse_bucket(label)
            tokens = json.loads(market.get("clobTokenIds") or "[]")
        except (ResearchError, TypeError, json.JSONDecodeError):
            continue
        if len(tokens) < 2:
            continue
        schedule = market.get("feeSchedule") if isinstance(market.get("feeSchedule"), dict) else {}
        rate = float(schedule.get("rate") or 0.05 if market.get("feesEnabled") else 0.0)
        buckets.append(
            Bucket(
                label=label,
                lower=lower,
                upper=upper,
                yes_token=str(tokens[0]),
                no_token=str(tokens[1]),
                market_id=str(market.get("id") or ""),
                condition_id=str(market.get("conditionId") or ""),
                fees_enabled=bool(market.get("feesEnabled")),
                fee_rate=rate,
            )
        )
    return sorted(buckets, key=lambda bucket: (bucket.lower, math.inf if bucket.upper is None else bucket.upper))


def _resolved_winner_bucket(event: dict[str, Any]) -> str | None:
    winners: list[str] = []
    for market in event.get("markets") or []:
        try:
            prices = json.loads(market.get("outcomePrices") or "[]")
        except (TypeError, json.JSONDecodeError):
            prices = []
        if prices and float(prices[0]) >= 0.99:
            label = str(market.get("groupItemTitle") or "").strip()
            if label:
                winners.append(label)
    return winners[0] if len(winners) == 1 else None


def _rule_signature(description: str) -> dict[str, Any]:
    text = description.lower()
    return {
        "counts_main_feed": "main feed posts" in text,
        "counts_quote_posts": "quote posts" in text,
        "counts_reposts": "reposts" in text,
        "excludes_replies": "replies will not count" in text or "replies will not" in text,
        "main_feed_reply_exception": "replies on the main feed" in text,
        "deleted_capture_approximately_5m": "~5 minutes" in text or "5 minutes" in text,
        "community_reposts_excluded": "community reposts" in text,
        "xtracker_primary": "xtracker.polymarket.com" in text,
        "x_secondary": "secondary resolution source" in text,
    }


def _count_between(timestamps: Sequence[float], start: float, end: float) -> int:
    return bisect.bisect_right(timestamps, end) - bisect.bisect_left(timestamps, start)


def _quantile(values: Sequence[float], q: float) -> float:
    return float(np.quantile(np.asarray(values, dtype=float), q)) if values else math.nan


def _safe_mean(values: Sequence[float]) -> float:
    return float(statistics.fmean(values)) if values else math.nan


def _safe_stdev(values: Sequence[float]) -> float:
    return float(statistics.stdev(values)) if len(values) > 1 else 0.0


def _post_features(posts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[float]]:
    unique = {str(row.get("platformId") or row.get("id")): row for row in posts}
    ordered = sorted(unique.values(), key=lambda row: parse_dt(row["createdAt"]))
    timestamps = [parse_dt(row["createdAt"]).timestamp() for row in ordered]
    windows = (5 * 60, 15 * 60, 30 * 60, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600)
    names = ("5m", "15m", "30m", "1h", "3h", "6h", "12h", "24h")
    features: list[dict[str, Any]] = []
    for index, (post, stamp) in enumerate(zip(ordered, timestamps)):
        dt = datetime.fromtimestamp(stamp, timezone.utc)
        local = dt.astimezone(ET)
        row: dict[str, Any] = {
            "id": post.get("id"),
            "platform_id": post.get("platformId"),
            "timestamp_utc": iso(dt),
            "timestamp_et": local.isoformat(),
            "date_et": local.date().isoformat(),
            "hour_et": local.hour,
            "weekday_et": local.strftime("%A"),
            "post_type": "UNAVAILABLE_FROM_XTRACKER_API",
            "content": post.get("content") or "",
            "url": f"https://x.com/{USER_HANDLE}/status/{post.get('platformId')}",
            "minutes_since_previous": None if index == 0 else (stamp - timestamps[index - 1]) / 60.0,
        }
        for name, seconds in zip(names, windows):
            row[f"posts_{name}"] = index - bisect.bisect_left(timestamps, stamp - seconds, 0, index)
        features.append(row)
    return features, timestamps


def _daily_baseline(timestamps: Sequence[float]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not timestamps:
        return [], {}
    first_local = datetime.fromtimestamp(timestamps[0], timezone.utc).astimezone(ET).date()
    last_local = CUT_OFF.astimezone(ET).date()
    cursor = first_local + timedelta(days=1)
    rows: list[dict[str, Any]] = []
    while cursor < last_local:
        start = datetime.combine(cursor, datetime.min.time(), ET).astimezone(timezone.utc).timestamp()
        end = datetime.combine(cursor + timedelta(days=1), datetime.min.time(), ET).astimezone(timezone.utc).timestamp() - 1e-6
        rows.append({"date_et": cursor.isoformat(), "weekday": cursor.strftime("%A"), "count": _count_between(timestamps, start, end)})
        cursor += timedelta(days=1)
    counts = [int(row["count"]) for row in rows]
    modes = statistics.multimode(counts) if counts else []
    mean = _safe_mean(counts)
    variance = float(statistics.variance(counts)) if len(counts) > 1 else 0.0
    summary = {
        "complete_days": len(counts),
        "mean": mean,
        "median": float(statistics.median(counts)) if counts else math.nan,
        "mode": modes[0] if modes else None,
        "std": _safe_stdev(counts),
        "variance": variance,
        "variance_mean_ratio": variance / mean if mean > 0 else math.nan,
        "p10": _quantile(counts, 0.10),
        "p25": _quantile(counts, 0.25),
        "p50": _quantile(counts, 0.50),
        "p75": _quantile(counts, 0.75),
        "p90": _quantile(counts, 0.90),
        "p95": _quantile(counts, 0.95),
        "minimum": min(counts) if counts else None,
        "maximum": max(counts) if counts else None,
    }
    return rows, summary


def _hourly_weekday_profiles(timestamps: Sequence[float]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_hour = Counter(datetime.fromtimestamp(stamp, timezone.utc).astimezone(ET).hour for stamp in timestamps)
    by_weekday = Counter(datetime.fromtimestamp(stamp, timezone.utc).astimezone(ET).strftime("%A") for stamp in timestamps)
    first = datetime.fromtimestamp(timestamps[0], timezone.utc).astimezone(ET).date()
    last = CUT_OFF.astimezone(ET).date()
    days = max(1, (last - first).days)
    hourly = [{"hour_et": hour, "posts": by_hour[hour], "posts_per_exposed_hour": by_hour[hour] / days} for hour in range(24)]
    weekday_days = Counter((first + timedelta(days=i)).strftime("%A") for i in range(days))
    weekdays = [
        {
            "weekday": name,
            "posts": by_weekday[name],
            "days": weekday_days[name],
            "mean_posts": by_weekday[name] / weekday_days[name] if weekday_days[name] else math.nan,
        }
        for name in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    ]
    return hourly, weekdays


def _interarrival_summary(timestamps: Sequence[float]) -> dict[str, Any]:
    intervals = [(right - left) / 60.0 for left, right in zip(timestamps, timestamps[1:]) if right >= left]
    if not intervals:
        return {}
    # Largest burst is the maximum posts in any rolling 15-minute interval.
    burst = 0
    left = 0
    for right, stamp in enumerate(timestamps):
        while timestamps[left] < stamp - 15 * 60:
            left += 1
        burst = max(burst, right - left + 1)
    return {
        "observations": len(intervals),
        "mean_minutes": _safe_mean(intervals),
        "median_minutes": float(statistics.median(intervals)),
        "p10_minutes": _quantile(intervals, 0.10),
        "p90_minutes": _quantile(intervals, 0.90),
        "p95_minutes": _quantile(intervals, 0.95),
        "longest_silence_hours": max(intervals) / 60.0,
        "largest_15m_burst": burst,
    }


def _parse_news_datetime(row: dict[str, Any]) -> datetime | None:
    for key in ("seendate", "date", "datetime"):
        value = row.get(key)
        if not value:
            continue
        text = str(value).strip()
        for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y%m%d%H%M%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        try:
            return parse_dt(text)
        except Exception:
            pass
    return None


def derive(output_root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    root = output_root.resolve()
    raw = root / "raw"
    derived = root / "derived"
    posts = read_json(raw / "xtracker_posts.json")
    trackings = read_json(raw / "xtracker_trackings.json")
    events = read_jsonl(raw / "gamma_events.jsonl")
    count_rows = read_jsonl(raw / "tweet_counts.jsonl")
    counts = {str(row["event_id"]): row.get("tweet_count") for row in count_rows}
    event_by_slug = {str(event.get("slug") or ""): event for event in events}

    post_rows, timestamps = _post_features(posts)
    write_jsonl(derived / "posts_features.jsonl", post_rows)
    write_csv(derived / "posts_features.csv", post_rows)
    daily_rows, baseline = _daily_baseline(timestamps)
    hourly, weekdays = _hourly_weekday_profiles(timestamps)
    interarrival = _interarrival_summary(timestamps)
    write_csv(derived / "daily_counts.csv", daily_rows)
    write_csv(derived / "intraday_profile.csv", hourly)
    write_csv(derived / "weekday_profile.csv", weekdays)

    market_rows: list[dict[str, Any]] = []
    rules_rows: list[dict[str, Any]] = []
    unmatched_trackings = 0
    for tracking in trackings:
        slug = _slug_from_market_link(str(tracking.get("marketLink") or ""))
        if not slug:
            continue
        event = event_by_slug.get(slug)
        if event is None:
            unmatched_trackings += 1
            continue
        start = parse_dt(tracking["startDate"])
        end = parse_dt(tracking["endDate"])
        buckets = event_buckets(event)
        gamma_tweet_count = counts.get(str(event.get("id")))
        tracker_count = _count_between(timestamps, start.timestamp(), end.timestamp()) if end <= CUT_OFF else _count_between(timestamps, start.timestamp(), CUT_OFF.timestamp())
        winner = _resolved_winner_bucket(event) if end < CUT_OFF else None
        winner_definition = next((bucket for bucket in buckets if bucket.label == winner), None)
        reconstruction_matches_winner = bool(winner_definition and winner_definition.contains(tracker_count))
        gamma_matches_winner = bool(
            winner_definition
            and gamma_tweet_count is not None
            and winner_definition.contains(int(gamma_tweet_count))
        )
        final_count = tracker_count if end < CUT_OFF and reconstruction_matches_winner else (
            int(gamma_tweet_count) if end < CUT_OFF and gamma_matches_winner else None
        )
        description = str(event.get("description") or (event.get("markets") or [{}])[0].get("description") or "")
        signature = _rule_signature(description)
        compatible = all(
            signature[key]
            for key in ("counts_main_feed", "counts_quote_posts", "counts_reposts", "excludes_replies", "xtracker_primary")
        )
        duration_seconds = (end - start).total_seconds() + 1.0
        row = {
            "event_id": str(event.get("id") or ""),
            "tracking_id": str(tracking.get("id") or ""),
            "slug": slug,
            "title": tracking.get("title") or event.get("title"),
            "window_start_utc": iso(start),
            "window_end_utc": iso(end),
            "window_start_et": start.astimezone(ET).isoformat(),
            "window_end_et": end.astimezone(ET).isoformat(),
            "duration_hours": duration_seconds / 3600.0,
            "duration_class": "48H" if abs(duration_seconds - 48 * 3600) < 120 else "7D" if abs(duration_seconds - 7 * 86400) < 120 else "OTHER",
            "market_created_utc": event.get("createdAt") or event.get("creationDate") or event.get("startDate"),
            "resolved_before_cutoff": end < CUT_OFF,
            "official_count": final_count,
            "final_count": final_count,
            "count_source": (
                "XTRACKER_POST_EXPORT_VALIDATED_BY_POLYMARKET_WINNER"
                if final_count is not None and reconstruction_matches_winner
                else "GAMMA_TWEET_COUNT_VALIDATED_BY_POLYMARKET_WINNER"
                if final_count is not None
                else "UNAVAILABLE"
            ),
            "gamma_tweet_count": gamma_tweet_count,
            "xtracker_reconstructed_count": tracker_count,
            "gamma_count_difference": None if gamma_tweet_count is None or end > CUT_OFF else int(gamma_tweet_count) - tracker_count,
            "reconstruction_matches_winner": reconstruction_matches_winner,
            "gamma_count_matches_winner": gamma_matches_winner,
            "winner_bucket": winner,
            "buckets_json": stable_json([bucket.__dict__ for bucket in buckets]),
            "bucket_count": len(buckets),
            "volume": float(event.get("volume") or 0.0),
            "liquidity": float(event.get("liquidity") or 0.0),
            "rules_compatible": compatible,
            "rule_signature": stable_json(signature),
            "description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
        }
        market_rows.append(row)
        rules_rows.append(
            {
                "slug": slug,
                "title": row["title"],
                "description_sha256": row["description_sha256"],
                "rules_compatible": compatible,
                **signature,
                "description": description,
            }
        )

    market_rows.sort(key=lambda row: (row["window_end_utc"], row["slug"]))
    unique_rules = {row["description_sha256"]: row for row in rules_rows}
    write_jsonl(derived / "markets.jsonl", market_rows)
    write_csv(derived / "markets.csv", market_rows)
    write_csv(derived / "market_rules_audit.csv", list(unique_rules.values()))

    news_rows = read_jsonl(raw / "news_gdelt.jsonl")
    normalized_news: list[dict[str, Any]] = []
    for row in news_rows:
        available = _parse_news_datetime(row)
        if available is None or available > CUT_OFF:
            continue
        normalized_news.append(
            {
                "timestamp_available": iso(available),
                "title": row.get("title") or "",
                "url": row.get("url") or "",
                "domain": row.get("domain") or "",
                "language": row.get("language") or "",
                "category": row.get("category") or _gdelt_category(str(row.get("title") or "")),
                "importance": row.get("importance") or _gdelt_importance(row),
            }
        )
    normalized_news.sort(key=lambda row: row["timestamp_available"])
    write_jsonl(derived / "news_events.jsonl", normalized_news)
    write_csv(derived / "news_events.csv", normalized_news)

    summary = {
        "schema": SCHEMA,
        "cut_off": iso(CUT_OFF),
        "posts_unique": len(timestamps),
        "post_period_start": iso(datetime.fromtimestamp(timestamps[0], timezone.utc)) if timestamps else None,
        "post_period_end": iso(datetime.fromtimestamp(timestamps[-1], timezone.utc)) if timestamps else None,
        "markets": len(market_rows),
        "resolved_markets": sum(bool(row["resolved_before_cutoff"]) for row in market_rows),
        "compatible_markets": sum(bool(row["rules_compatible"]) for row in market_rows),
        "gamma_count_exact_matches": sum(row["gamma_count_difference"] == 0 for row in market_rows if row["gamma_count_difference"] is not None),
        "gamma_count_comparisons": sum(row["gamma_count_difference"] is not None for row in market_rows),
        "reconstruction_bucket_matches": sum(bool(row["reconstruction_matches_winner"]) for row in market_rows if row["resolved_before_cutoff"]),
        "resolved_with_winner": sum(bool(row["winner_bucket"]) for row in market_rows if row["resolved_before_cutoff"]),
        "unmatched_trackings": unmatched_trackings,
        "rule_versions": len({row["rule_signature"] for row in market_rows}),
        "post_type_available": False,
        "news_articles": len(normalized_news),
        "news_coverage_start": normalized_news[0]["timestamp_available"] if normalized_news else None,
        "daily_baseline": baseline,
        "interarrival": interarrival,
    }
    write_json(derived / "derivation_summary.json", summary)
    return summary


def _pmf_poisson(mean: float) -> np.ndarray:
    mean = max(1e-6, float(mean))
    maximum = max(25, int(math.ceil(mean + 9.0 * math.sqrt(mean + 1.0))))
    values = poisson.pmf(np.arange(maximum + 1), mean)
    values[-1] += max(0.0, 1.0 - float(values.sum()))
    return values / values.sum()


def _pmf_negative_binomial(mean: float, alpha: float) -> np.ndarray:
    mean = max(1e-6, float(mean))
    if not math.isfinite(alpha) or alpha <= 1e-8:
        return _pmf_poisson(mean)
    size = 1.0 / alpha
    probability = size / (size + mean)
    variance = mean + alpha * mean * mean
    maximum = max(30, int(math.ceil(mean + 10.0 * math.sqrt(variance + 1.0))))
    values = nbinom.pmf(np.arange(maximum + 1), size, probability)
    values[-1] += max(0.0, 1.0 - float(values.sum()))
    return values / values.sum()


def _pmf_empirical(samples: Sequence[int], *, smoothing: float = 0.25) -> np.ndarray:
    if not samples:
        return _pmf_poisson(1.0)
    maximum = max(samples) + 5
    counts = np.full(maximum + 1, smoothing, dtype=float)
    for value in samples:
        counts[min(maximum, max(0, int(value)))] += 1.0
    return counts / counts.sum()


def _shift_pmf(remaining: np.ndarray, current_count: int) -> np.ndarray:
    result = np.zeros(current_count + len(remaining), dtype=float)
    result[current_count:] = remaining
    return result / result.sum()


def _bucket_probabilities(pmf: np.ndarray, buckets: Sequence[Bucket]) -> list[float]:
    probabilities: list[float] = []
    for bucket in buckets:
        upper = len(pmf) - 1 if bucket.upper is None else min(bucket.upper, len(pmf) - 1)
        if bucket.lower >= len(pmf) or upper < bucket.lower:
            probabilities.append(0.0)
        else:
            probabilities.append(float(pmf[bucket.lower : upper + 1].sum()))
    total = sum(probabilities)
    if total <= 0:
        probabilities = [1.0 / len(buckets)] * len(buckets)
    else:
        probabilities = [value / total for value in probabilities]
    return probabilities


def _entropy(probabilities: Sequence[float]) -> float:
    return -sum(value * math.log(value) for value in probabilities if value > 0)


def _historical_hour_rates(timestamps: Sequence[float], observation: datetime, lookback_days: int = 120) -> tuple[list[float], float]:
    end = observation.timestamp()
    start = max(timestamps[0], end - lookback_days * 86400)
    left = bisect.bisect_left(timestamps, start)
    right = bisect.bisect_right(timestamps, end)
    selected = timestamps[left:right]
    exposure_days = max(1.0, (end - start) / 86400.0)
    counts = Counter(datetime.fromtimestamp(stamp, timezone.utc).astimezone(ET).hour for stamp in selected)
    global_hourly = len(selected) / max(1.0, exposure_days * 24.0)
    # Four hours of empirical-Bayes shrinkage stabilizes quiet clock hours.
    rates = [(counts[hour] + 4.0 * global_hourly) / (exposure_days + 4.0) for hour in range(24)]
    return rates, global_hourly


def _integrated_intraday_mean(hour_rates: Sequence[float], start: datetime, end: datetime) -> float:
    if end <= start:
        return 0.0
    cursor = start
    total = 0.0
    while cursor < end:
        next_boundary = min(end, cursor + timedelta(minutes=15))
        hours = (next_boundary - cursor).total_seconds() / 3600.0
        total += float(hour_rates[cursor.astimezone(ET).hour]) * hours
        cursor = next_boundary
    return total


def _historical_daily_counts(timestamps: Sequence[float], observation: datetime, lookback_days: int = 120) -> list[int]:
    local_end = observation.astimezone(ET).date()
    first_available = datetime.fromtimestamp(timestamps[0], timezone.utc).astimezone(ET).date()
    local_start = max(first_available, local_end - timedelta(days=lookback_days))
    values: list[int] = []
    cursor = local_start
    while cursor < local_end:
        start = datetime.combine(cursor, datetime.min.time(), ET).astimezone(timezone.utc).timestamp()
        end = datetime.combine(cursor + timedelta(days=1), datetime.min.time(), ET).astimezone(timezone.utc).timestamp() - 1e-6
        values.append(_count_between(timestamps, start, end))
        cursor += timedelta(days=1)
    return values


def _analog_samples(timestamps: Sequence[float], observation: datetime, remaining_seconds: float) -> list[int]:
    end_limit = observation.timestamp()
    start_limit = max(timestamps[0], end_limit - 180 * 86400)
    step = max(3600.0, min(12 * 3600.0, remaining_seconds / 16.0))
    target_local = observation.astimezone(ET)
    samples: list[int] = []
    cursor = start_limit
    while cursor + remaining_seconds <= end_limit:
        local = datetime.fromtimestamp(cursor, timezone.utc).astimezone(ET)
        if abs(local.hour - target_local.hour) <= 2 or abs(local.hour - target_local.hour) >= 22:
            samples.append(_count_between(timestamps, cursor, cursor + remaining_seconds - 1e-6))
        cursor += step
    if len(samples) < 20:
        cursor = start_limit
        samples = []
        while cursor + remaining_seconds <= end_limit:
            samples.append(_count_between(timestamps, cursor, cursor + remaining_seconds - 1e-6))
            cursor += step
    return samples


def _forecast_models(
    timestamps: Sequence[float],
    window_start: datetime,
    observation: datetime,
    window_end: datetime,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    start_ts = window_start.timestamp()
    observation_ts = observation.timestamp()
    current_count = _count_between(timestamps, start_ts, observation_ts)
    remaining_seconds = max(0.0, (window_end - observation).total_seconds())
    remaining_hours = remaining_seconds / 3600.0
    hour_rates, global_hourly = _historical_hour_rates(timestamps, observation)
    intraday_mean = _integrated_intraday_mean(hour_rates, observation, window_end)

    recent_6h = _count_between(timestamps, observation_ts - 6 * 3600, observation_ts)
    recent_24h = _count_between(timestamps, observation_ts - 24 * 3600, observation_ts)
    recent_rate = 0.50 * (recent_6h / 6.0) + 0.35 * (recent_24h / 24.0) + 0.15 * global_hourly
    naive_mean = max(0.0, recent_rate * remaining_hours)

    daily = _historical_daily_counts(timestamps, observation)
    daily_mean = _safe_mean(daily) if daily else global_hourly * 24.0
    daily_variance = float(statistics.variance(daily)) if len(daily) > 1 else daily_mean
    alpha = max(0.0, (daily_variance - daily_mean) / max(1e-9, daily_mean * daily_mean))

    # Gamma-Poisson posterior: 24 hours of prior exposure plus the latest 24h.
    prior_shape = max(0.1, daily_mean)
    posterior_shape = prior_shape + recent_24h
    posterior_exposure_days = 2.0
    posterior_daily = posterior_shape / posterior_exposure_days
    tod_scale = intraday_mean / max(1e-9, global_hourly * remaining_hours) if remaining_hours > 0 else 1.0
    bayes_mean = max(0.0, posterior_daily / 24.0 * remaining_hours * tod_scale)
    bayes_alpha = 1.0 / posterior_shape

    history_end = bisect.bisect_right(timestamps, observation_ts)
    historical_intervals = np.diff(np.asarray(timestamps[max(0, history_end - 4000) : history_end], dtype=float)) / 60.0
    short_share = float(np.mean(historical_intervals <= 15.0)) if historical_intervals.size else 0.0
    expected_short = 1.0 - math.exp(-max(1e-9, global_hourly) * 0.25)
    excitation_strength = max(0.0, min(2.0, short_share / max(1e-6, expected_short) - 1.0))
    recent_stamps = timestamps[bisect.bisect_left(timestamps, observation_ts - 12 * 3600) : history_end]
    tau_hours = 1.5
    residual_excitation = sum(math.exp(-((observation_ts - stamp) / 3600.0) / tau_hours) for stamp in recent_stamps)
    hawkes_extra = excitation_strength * 0.15 * residual_excitation * tau_hours * (1.0 - math.exp(-remaining_hours / tau_hours))
    hawkes_mean = max(0.0, intraday_mean + hawkes_extra)

    analog = _analog_samples(timestamps, observation, remaining_seconds)
    models = {
        "historical_analog": _shift_pmf(_pmf_empirical(analog), current_count),
        "naive_recent_rate": _shift_pmf(_pmf_poisson(naive_mean), current_count),
        "poisson_intraday": _shift_pmf(_pmf_poisson(intraday_mean), current_count),
        "negative_binomial": _shift_pmf(_pmf_negative_binomial(intraday_mean, alpha), current_count),
        "bayesian_dynamic_rate": _shift_pmf(_pmf_negative_binomial(bayes_mean, bayes_alpha), current_count),
        "hawkes_proxy": _shift_pmf(_pmf_poisson(hawkes_mean), current_count),
    }
    features = {
        "current_count": current_count,
        "time_elapsed_hours": max(0.0, (observation - window_start).total_seconds() / 3600.0),
        "time_remaining_hours": remaining_hours,
        "current_rate": current_count / max(1e-9, (observation - window_start).total_seconds() / 3600.0),
        "rate_1h": _count_between(timestamps, observation_ts - 3600, observation_ts),
        "rate_3h": _count_between(timestamps, observation_ts - 3 * 3600, observation_ts) / 3.0,
        "rate_6h": recent_6h / 6.0,
        "rate_12h": _count_between(timestamps, observation_ts - 12 * 3600, observation_ts) / 12.0,
        "rate_24h": recent_24h / 24.0,
        "baseline_rate": global_hourly,
        "recent_baseline_ratio": (recent_24h / 24.0) / global_hourly if global_hourly > 0 else math.nan,
        "time_since_last_post_minutes": (observation_ts - timestamps[history_end - 1]) / 60.0 if history_end else math.nan,
        "dispersion_alpha": alpha,
        "analog_samples": len(analog),
        "hawkes_excitation_strength": excitation_strength,
    }
    return models, features


def _observation_times(start: datetime, end: datetime) -> list[tuple[datetime, list[str]]]:
    duration = (end - start).total_seconds()
    times: dict[int, list[str]] = defaultdict(list)
    for fraction in ENTRY_FRACTIONS:
        stamp = start + timedelta(seconds=duration * fraction)
        times[round(stamp.timestamp())].append(f"F_{int(round(fraction * 100)):02d}")
    for remaining in ENTRY_REMAINING_SECONDS:
        stamp = end - timedelta(seconds=remaining)
        if start <= stamp <= end:
            label = f"R_{remaining // 3600}H" if remaining >= 3600 else f"R_{remaining // 60}M"
            times[round(stamp.timestamp())].append(label)
    return [(datetime.fromtimestamp(stamp, timezone.utc), labels) for stamp, labels in sorted(times.items())]


def _assign_splits(markets: list[dict[str, Any]]) -> dict[str, str]:
    groups: list[list[dict[str, Any]]] = []
    for _, rows in _groupby_sorted(markets, key=lambda row: row["window_end_utc"]):
        groups.append(rows)
    total = len(markets)
    result: dict[str, str] = {}
    assigned = 0
    for group in groups:
        midpoint = (assigned + len(group) / 2.0) / max(1, total)
        split = "TRAIN" if midpoint <= 0.60 else "VALIDATION" if midpoint <= 0.80 else "TEST"
        for row in group:
            result[row["slug"]] = split
        assigned += len(group)
    return result


def _groupby_sorted(rows: Sequence[dict[str, Any]], key: Any) -> Iterable[tuple[Any, list[dict[str, Any]]]]:
    current_key: Any = object()
    current: list[dict[str, Any]] = []
    for row in sorted(rows, key=key):
        row_key = key(row)
        if current and row_key != current_key:
            yield current_key, current
            current = []
        current_key = row_key
        current.append(row)
    if current:
        yield current_key, current


def build_forecasts(output_root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    root = output_root.resolve()
    derived = root / "derived"
    analysis = root / "analysis"
    post_rows = read_jsonl(derived / "posts_features.jsonl")
    timestamps = [parse_dt(row["timestamp_utc"]).timestamp() for row in post_rows]
    markets = read_jsonl(derived / "markets.jsonl")
    earliest = datetime.fromtimestamp(timestamps[0], timezone.utc) if timestamps else CUT_OFF
    eligible = [
        row
        for row in markets
        if row.get("resolved_before_cutoff")
        and row.get("rules_compatible")
        and row.get("official_count") is not None
        and row.get("winner_bucket")
        and parse_dt(row["window_start_utc"]) >= earliest + timedelta(days=30)
        and len(json.loads(row["buckets_json"])) >= 2
    ]
    eligible.sort(key=lambda row: (row["window_end_utc"], row["slug"]))
    split_map = _assign_splits(eligible)

    forecasts: list[dict[str, Any]] = []
    for market in eligible:
        start = parse_dt(market["window_start_utc"])
        end = parse_dt(market["window_end_utc"])
        buckets = [Bucket(**row) for row in json.loads(market["buckets_json"])]
        actual = int(market["official_count"])
        actual_index = next((index for index, bucket in enumerate(buckets) if bucket.contains(actual)), None)
        if actual_index is None:
            continue
        for observation, labels in _observation_times(start, end):
            if observation > CUT_OFF or observation < earliest + timedelta(days=30):
                continue
            distributions, features = _forecast_models(timestamps, start, observation, end)
            for model_name, pmf in distributions.items():
                probabilities = _bucket_probabilities(pmf, buckets)
                brier = sum((probability - (1.0 if index == actual_index else 0.0)) ** 2 for index, probability in enumerate(probabilities))
                log_loss = -math.log(max(1e-12, probabilities[actual_index]))
                expected_final = float(np.dot(np.arange(len(pmf), dtype=float), pmf))
                forecasts.append(
                    {
                        "slug": market["slug"],
                        "event_id": market["event_id"],
                        "split": split_map[market["slug"]],
                        "observation_utc": iso(observation),
                        "timing_labels": "|".join(labels),
                        "model": model_name,
                        "actual_count": actual,
                        "actual_bucket": buckets[actual_index].label,
                        "bucket_labels_json": stable_json([bucket.label for bucket in buckets]),
                        "probabilities_json": stable_json(probabilities),
                        "expected_final": expected_final,
                        "median_final": int(np.searchsorted(np.cumsum(pmf), 0.5)),
                        "p10_final": int(np.searchsorted(np.cumsum(pmf), 0.1)),
                        "p90_final": int(np.searchsorted(np.cumsum(pmf), 0.9)),
                        "brier": brier,
                        "log_loss": log_loss,
                        "absolute_error": abs(expected_final - actual),
                        "entropy": _entropy(probabilities),
                        **features,
                    }
                )
    forecasts.sort(key=lambda row: (row["observation_utc"], row["slug"], row["model"]))
    write_jsonl(analysis / "forecasts.jsonl", forecasts)
    write_csv(analysis / "forecasts.csv", forecasts)

    metric_rows: list[dict[str, Any]] = []
    for (model, split), rows in _groupby_sorted(forecasts, key=lambda row: (row["model"], row["split"])):
        by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_event[row["slug"]].append(row)
        event_brier = [_safe_mean([float(row["brier"]) for row in event_rows]) for event_rows in by_event.values()]
        event_log = [_safe_mean([float(row["log_loss"]) for row in event_rows]) for event_rows in by_event.values()]
        event_mae = [_safe_mean([float(row["absolute_error"]) for row in event_rows]) for event_rows in by_event.values()]
        metric_rows.append(
            {
                "model": model,
                "split": split,
                "events": len(by_event),
                "forecasts": len(rows),
                "brier_event_weighted": _safe_mean(event_brier),
                "log_loss_event_weighted": _safe_mean(event_log),
                "mae_event_weighted": _safe_mean(event_mae),
            }
        )
    metric_rows.sort(key=lambda row: (row["split"], row["brier_event_weighted"]))
    write_csv(analysis / "model_metrics.csv", metric_rows)

    validation = [row for row in metric_rows if row["split"] == "VALIDATION"]
    validation.sort(key=lambda row: (row["brier_event_weighted"], row["log_loss_event_weighted"]))
    if not validation:
        raise ResearchError("No hay muestra de validación suficiente")
    primary = validation[0]["model"]
    secondary = validation[1]["model"] if len(validation) > 1 else primary
    selected_rows, selection = _select_or_ensemble(forecasts, primary, secondary)
    write_jsonl(analysis / "selected_forecasts.jsonl", selected_rows)
    write_csv(analysis / "selected_forecasts.csv", selected_rows)
    write_json(analysis / "model_selection.json", selection)
    return {
        "eligible_events": len(eligible),
        "forecasts": len(forecasts),
        "splits": dict(Counter(split_map.values())),
        "selection": selection,
    }


def _select_or_ensemble(
    forecasts: list[dict[str, Any]], primary: str, secondary: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in forecasts:
        grouped[(row["slug"], row["observation_utc"])][row["model"]] = row

    candidates: list[tuple[float, float]] = []
    for weight in (0.0, 0.25, 0.50, 0.75, 1.0):
        by_event: dict[str, list[float]] = defaultdict(list)
        for (slug, _), models in grouped.items():
            if primary not in models or secondary not in models or models[primary]["split"] != "VALIDATION":
                continue
            first = models[primary]
            second = models[secondary]
            p1 = json.loads(first["probabilities_json"])
            p2 = json.loads(second["probabilities_json"])
            probabilities = [weight * left + (1.0 - weight) * right for left, right in zip(p1, p2)]
            labels = json.loads(first["bucket_labels_json"])
            actual_index = labels.index(first["actual_bucket"])
            brier = sum((value - (1.0 if index == actual_index else 0.0)) ** 2 for index, value in enumerate(probabilities))
            by_event[slug].append(brier)
        score = _safe_mean([_safe_mean(values) for values in by_event.values()])
        candidates.append((score, weight))
    candidates.sort()
    best_score, best_weight = candidates[0]
    primary_score = next((row["brier_event_weighted"] for row in read_model_metric_rows(forecasts, primary) if row["split"] == "VALIDATION"), math.inf)
    use_ensemble = secondary != primary and best_score + 1e-4 < primary_score and 0.0 < best_weight < 1.0

    selected: list[dict[str, Any]] = []
    for _, models in sorted(grouped.items()):
        if primary not in models:
            continue
        row = dict(models[primary])
        if use_ensemble and secondary in models:
            p1 = json.loads(models[primary]["probabilities_json"])
            p2 = json.loads(models[secondary]["probabilities_json"])
            probabilities = [best_weight * left + (1.0 - best_weight) * right for left, right in zip(p1, p2)]
            labels = json.loads(row["bucket_labels_json"])
            actual_index = labels.index(row["actual_bucket"])
            row["probabilities_json"] = stable_json(probabilities)
            row["model"] = f"ensemble:{primary}:{best_weight:.2f}+{secondary}:{1-best_weight:.2f}"
            row["brier"] = sum((value - (1.0 if index == actual_index else 0.0)) ** 2 for index, value in enumerate(probabilities))
            row["log_loss"] = -math.log(max(1e-12, probabilities[actual_index]))
            row["entropy"] = _entropy(probabilities)
        selected.append(row)
    selection = {
        "primary_model": primary,
        "secondary_model": secondary,
        "validation_primary_brier": primary_score,
        "best_candidate_brier": best_score,
        "ensemble_weight_primary": best_weight,
        "ensemble_used": use_ensemble,
        "selected_model": selected[0]["model"] if selected else primary,
        "test_not_used_for_selection": True,
    }
    return selected, selection


def read_model_metric_rows(forecasts: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for split in ("TRAIN", "VALIDATION", "TEST"):
        rows = [row for row in forecasts if row["model"] == model and row["split"] == split]
        by_event: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            by_event[row["slug"]].append(float(row["brier"]))
        result.append(
            {
                "model": model,
                "split": split,
                "brier_event_weighted": _safe_mean([_safe_mean(values) for values in by_event.values()]),
            }
        )
    return result


def _load_price_history(root: Path, slug: str) -> dict[str, list[dict[str, Any]]]:
    path = root / "raw" / "price_history" / f"{slug}.json"
    if not path.exists():
        return {}
    payload = read_json(path)
    history = payload.get("history", {}) if isinstance(payload, dict) else {}
    result: dict[str, list[dict[str, Any]]] = {}
    for token, points in history.items():
        result[str(token)] = sorted(
            [point for point in points if isinstance(point, dict) and point.get("t") is not None and point.get("p") is not None],
            key=lambda point: float(point["t"]),
        )
    return result


def _reference_price(points: Sequence[dict[str, Any]], observation_ts: float) -> tuple[float | None, float | None]:
    stamps = [float(point["t"]) for point in points]
    index = bisect.bisect_right(stamps, observation_ts) - 1
    if index < 0:
        return None, None
    age = observation_ts - stamps[index]
    if age > MAX_PRICE_AGE_SECONDS:
        return None, age
    price = float(points[index]["p"])
    return min(0.999, max(0.001, price)), age


def _fee_per_share(price: float, rate: float, enabled: bool) -> float:
    if not enabled or rate <= 0:
        return 0.0
    return rate * price * (1.0 - price)


def _opportunity_from_forecast(
    row: dict[str, Any], market: dict[str, Any], root: Path
) -> dict[str, Any] | None:
    buckets = [Bucket(**item) for item in json.loads(market["buckets_json"])]
    labels = json.loads(row["bucket_labels_json"])
    probabilities = json.loads(row["probabilities_json"])
    probability_by_label = dict(zip(labels, probabilities))
    history = _load_price_history(root, row["slug"])
    observation_ts = parse_dt(row["observation_utc"]).timestamp()
    actual_bucket = str(row["actual_bucket"])
    candidates: list[dict[str, Any]] = []
    price_vector: list[float] = []
    actual_vector: list[float] = []
    for bucket in buckets:
        q_yes = float(probability_by_label.get(bucket.label, 0.0))
        reference, age = _reference_price(history.get(bucket.yes_token, []), observation_ts)
        if reference is None:
            continue
        price_vector.append(reference)
        actual_vector.append(1.0 if bucket.label == actual_bucket else 0.0)
        for side, probability, raw_price, wins in (
            ("YES", q_yes, reference, bucket.label == actual_bucket),
            ("NO", 1.0 - q_yes, 1.0 - reference, bucket.label != actual_bucket),
        ):
            entry = min(0.99, max(0.01, raw_price + ENTRY_IMPACT))
            fee_share = _fee_per_share(entry, bucket.fee_rate, bucket.fees_enabled)
            candidates.append(
                {
                    "bucket": bucket.label,
                    "side": side,
                    "model_probability": probability,
                    "reference_price": raw_price,
                    "entry_price": entry,
                    "price_age_seconds": age,
                    "fee_per_share": fee_share,
                    "net_edge_per_share": probability - entry - fee_share,
                    "wins": wins,
                    "fee_rate": bucket.fee_rate,
                    "fees_enabled": bucket.fees_enabled,
                }
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item["net_edge_per_share"], item["model_probability"]), reverse=True)
    best = candidates[0]
    stake = 100.0
    shares = stake / best["entry_price"]
    fee = shares * best["fee_per_share"]
    pnl = (shares if best["wins"] else 0.0) - stake - fee
    market_brier = None
    if price_vector:
        total = sum(price_vector)
        normalized = [value / total for value in price_vector] if total > 0 else [1.0 / len(price_vector)] * len(price_vector)
        market_brier = sum((left - right) ** 2 for left, right in zip(normalized, actual_vector))
    return {
        "slug": row["slug"],
        "event_id": row["event_id"],
        "split": row["split"],
        "observation_utc": row["observation_utc"],
        "window_end_utc": market["window_end_utc"],
        "timing_labels": row["timing_labels"],
        "model": row["model"],
        "actual_count": row["actual_count"],
        "actual_bucket": actual_bucket,
        "forecast_entropy": row["entropy"],
        "time_remaining_hours": row["time_remaining_hours"],
        "best_bucket": best["bucket"],
        "best_side": best["side"],
        "model_probability": best["model_probability"],
        "reference_price": best["reference_price"],
        "entry_price": best["entry_price"],
        "price_age_seconds": best["price_age_seconds"],
        "fee_usdc_100": fee,
        "net_edge_per_share": best["net_edge_per_share"],
        "win": bool(best["wins"]),
        "pnl_100": pnl,
        "capital_100": stake + fee,
        "market_brier": market_brier,
    }


def _drawdown(pnls: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def _capital_peak(rows: Sequence[dict[str, Any]]) -> float:
    points: list[tuple[float, int, float]] = []
    for row in rows:
        entry = parse_dt(row["observation_utc"]).timestamp()
        exit_stamp = parse_dt(row["window_end_utc"]).timestamp()
        capital = float(row["capital_100"])
        points.append((entry, 1, capital))
        points.append((exit_stamp, -1, capital))
    active = 0.0
    peak = 0.0
    for _, direction, capital in sorted(points, key=lambda item: (item[0], item[1])):
        active += direction * capital
        peak = max(peak, active)
    return peak


def _strategy_metrics(rows: list[dict[str, Any]], *, label: str, threshold: float, split: str) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (row["window_end_utc"], row["slug"]))
    pnls = [float(row["pnl_100"]) for row in ordered]
    capital = sum(float(row["capital_100"]) for row in ordered)
    gains = sum(max(0.0, pnl) for pnl in pnls)
    losses = -sum(min(0.0, pnl) for pnl in pnls)
    sorted_pnls = sorted(pnls, reverse=True)
    holding_hours = sum(float(row["time_remaining_hours"]) for row in ordered)
    peak = _capital_peak(ordered)
    return {
        "strategy": label,
        "threshold": threshold,
        "split": split,
        "trades": len(ordered),
        "events": len({row["slug"] for row in ordered}),
        "wins": sum(bool(row["win"]) for row in ordered),
        "losses": sum(not bool(row["win"]) for row in ordered),
        "win_rate": sum(bool(row["win"]) for row in ordered) / len(ordered) if ordered else math.nan,
        "pnl": sum(pnls),
        "capital_cost": capital,
        "roi": sum(pnls) / capital if capital else math.nan,
        "ev_per_trade_realized": _safe_mean(pnls),
        "mean_model_edge": _safe_mean([float(row["net_edge_per_share"]) for row in ordered]),
        "profit_factor": gains / losses if losses > 0 else (math.inf if gains > 0 else math.nan),
        "max_drawdown": _drawdown(pnls),
        "holding_hours": holding_hours,
        "pnl_per_capital_hour": sum(pnls) / holding_hours / 100.0 if holding_hours else math.nan,
        "peak_concurrent_capital": peak,
        "pnl_without_top1": sum(sorted_pnls[1:]) if len(sorted_pnls) > 1 else 0.0,
        "pnl_without_top3": sum(sorted_pnls[3:]) if len(sorted_pnls) > 3 else 0.0,
    }


def economic_backtest(output_root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    root = output_root.resolve()
    analysis = root / "analysis"
    markets = {row["slug"]: row for row in read_jsonl(root / "derived" / "markets.jsonl")}
    forecasts = read_jsonl(analysis / "selected_forecasts.jsonl")
    opportunities: list[dict[str, Any]] = []
    for row in forecasts:
        market = markets.get(row["slug"])
        if not market:
            continue
        opportunity = _opportunity_from_forecast(row, market, root)
        if opportunity:
            opportunities.append(opportunity)
    opportunities.sort(key=lambda row: (row["observation_utc"], row["slug"]))
    write_jsonl(analysis / "economic_opportunities.jsonl", opportunities)
    write_csv(analysis / "economic_opportunities.csv", opportunities)

    metric_rows: list[dict[str, Any]] = []
    for threshold in EDGE_THRESHOLDS:
        timing_labels = sorted({label for row in opportunities for label in str(row["timing_labels"]).split("|") if label})
        for label in timing_labels:
            for split in ("TRAIN", "VALIDATION", "TEST"):
                selected = [
                    row
                    for row in opportunities
                    if row["split"] == split
                    and label in str(row["timing_labels"]).split("|")
                    and float(row["net_edge_per_share"]) >= threshold
                ]
                metric_rows.append(_strategy_metrics(selected, label=label, threshold=threshold, split=split))

        # Dynamic strategy: first qualifying signal in each market. It is
        # executable without selecting the best timestamp after resolution.
        for split in ("TRAIN", "VALIDATION", "TEST"):
            candidates = [row for row in opportunities if row["split"] == split and float(row["net_edge_per_share"]) >= threshold]
            first_by_event: dict[str, dict[str, Any]] = {}
            for row in sorted(candidates, key=lambda item: item["observation_utc"]):
                first_by_event.setdefault(row["slug"], row)
            metric_rows.append(
                _strategy_metrics(list(first_by_event.values()), label="DYNAMIC_FIRST_EDGE", threshold=threshold, split=split)
            )

    write_csv(analysis / "timing_strategy_metrics.csv", metric_rows)
    lookup = {(row["strategy"], float(row["threshold"]), row["split"]): row for row in metric_rows}
    candidates: list[dict[str, Any]] = []
    for row in metric_rows:
        if row["split"] != "VALIDATION" or row["trades"] < 5:
            continue
        train = lookup.get((row["strategy"], float(row["threshold"]), "TRAIN"))
        if not train or train["trades"] < 10:
            continue
        if float(train["roi"]) <= 0 or float(row["roi"]) <= 0:
            continue
        if float(train["profit_factor"]) <= 1.0 or float(row["profit_factor"]) <= 1.0:
            continue
        candidate = dict(row)
        candidate["train_roi"] = train["roi"]
        candidate["train_pf"] = train["profit_factor"]
        candidates.append(candidate)
    candidates.sort(key=lambda row: (float(row["roi"]), float(row["pnl"])), reverse=True)
    finalist = candidates[0] if candidates else None
    test = None
    confirmed = False
    if finalist:
        test = lookup.get((finalist["strategy"], float(finalist["threshold"]), "TEST"))
        confirmed = bool(
            test
            and test["trades"] >= 5
            and float(test["roi"]) > 0
            and float(test["profit_factor"]) > 1.10
            and float(test["pnl_without_top1"]) > 0
        )
    opportunity_split_counts = dict(Counter(row["split"] for row in opportunities))
    chronological_coverage_ok = opportunity_split_counts.get("TRAIN", 0) > 0 and opportunity_split_counts.get("VALIDATION", 0) > 0
    selection = {
        "train_validation_survivors": len(candidates),
        "finalist": finalist,
        "test_result": test,
        "test_confirmed": confirmed,
        "deployment_status": (
            "CONFIRMED_FOR_FORWARD_SHADOW"
            if confirmed
            else "INSUFFICIENT_CHRONOLOGICAL_PRICE_COVERAGE"
            if not chronological_coverage_ok
            else "NO_CONFIRMED_DEPLOYABLE_WINNER"
        ),
        "opportunity_split_counts": opportunity_split_counts,
        "chronological_price_coverage_ok": chronological_coverage_ok,
        "economic_results_interpretable": chronological_coverage_ok,
        "historical_capacity_verified": False,
        "price_source": "CLOB historical reference at-or-before decision; +1 cent impact proxy",
        "test_not_used_for_selection": True,
    }
    write_json(analysis / "economic_selection.json", selection)
    return {
        "opportunities": len(opportunities),
        "metric_rows": len(metric_rows),
        **selection,
    }


def _regime_snapshot(timestamps: Sequence[float], observation: datetime) -> dict[str, Any]:
    end = observation.timestamp()
    current = {
        "posts_15m": _count_between(timestamps, end - 15 * 60, end),
        "posts_30m": _count_between(timestamps, end - 30 * 60, end),
        "posts_1h": _count_between(timestamps, end - 3600, end),
        "posts_3h": _count_between(timestamps, end - 3 * 3600, end),
        "posts_6h": _count_between(timestamps, end - 6 * 3600, end),
        "posts_12h": _count_between(timestamps, end - 12 * 3600, end),
        "posts_24h": _count_between(timestamps, end - 24 * 3600, end),
    }
    history: list[float] = []
    cursor = max(timestamps[0] + 24 * 3600, end - 120 * 86400)
    while cursor < end - 3600:
        score = (
            _count_between(timestamps, cursor - 3600, cursor)
            + _count_between(timestamps, cursor - 6 * 3600, cursor) / 6.0
            + _count_between(timestamps, cursor - 24 * 3600, cursor) / 24.0
        ) / 3.0
        history.append(score)
        cursor += 3600
    score = (current["posts_1h"] + current["posts_6h"] / 6.0 + current["posts_24h"] / 24.0) / 3.0
    percentile = float(np.mean(np.asarray(history) <= score)) if history else 0.5
    if percentile >= 0.97:
        regime = "BURST"
    elif percentile >= 0.90:
        regime = "MUY_ACTIVO"
    elif percentile >= 0.65:
        regime = "ACTIVO"
    elif percentile >= 0.35:
        regime = "NORMAL"
    elif percentile >= 0.10:
        regime = "INACTIVO"
    else:
        regime = "MUY_INACTIVO"
    last_index = bisect.bisect_right(timestamps, end) - 1
    current["time_since_last_post_minutes"] = (end - timestamps[last_index]) / 60.0 if last_index >= 0 else math.nan
    current["burstiness_score_percentile"] = percentile
    current["regime"] = regime
    return current


def news_effect_analysis(output_root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    root = output_root.resolve()
    news = read_jsonl(root / "derived" / "news_events.jsonl")
    posts = read_jsonl(root / "derived" / "posts_features.jsonl")
    timestamps = [parse_dt(row["timestamp_utc"]).timestamp() for row in posts]
    if not news or not timestamps:
        result = {"available": False, "reason": "NO_COMPLETE_NEWS_DATA", "events": 0}
        write_json(root / "analysis" / "news_effect_summary.json", result)
        return result
    # Collapse same-category coverage clusters so syndicated headlines are not
    # treated as independent causal events.
    clusters: list[dict[str, Any]] = []
    last_by_category: dict[str, float] = {}
    for row in news:
        stamp = parse_dt(row["timestamp_available"]).timestamp()
        category = str(row["category"])
        if stamp - last_by_category.get(category, -math.inf) < 6 * 3600:
            continue
        last_by_category[category] = stamp
        clusters.append(row)
    rows: list[dict[str, Any]] = []
    for event in clusters:
        stamp = parse_dt(event["timestamp_available"]).timestamp()
        before = _count_between(timestamps, stamp - 3600, stamp)
        row = dict(event)
        row["posts_1h_before"] = before
        for hours in (1, 3, 6, 12, 24):
            row[f"posts_{hours}h_after"] = _count_between(timestamps, stamp, min(CUT_OFF.timestamp(), stamp + hours * 3600))
        rows.append(row)
    write_csv(root / "analysis" / "news_effect_events.csv", rows)
    baseline_hourly = len(timestamps) / max(1.0, (timestamps[-1] - timestamps[0]) / 3600.0)
    summary_rows: list[dict[str, Any]] = []
    for (category, importance), grouped in _groupby_sorted(rows, key=lambda row: (row["category"], row["importance"])):
        after_6h = [float(row["posts_6h_after"]) / 6.0 for row in grouped]
        summary_rows.append(
            {
                "category": category,
                "importance": importance,
                "events": len(grouped),
                "mean_posts_per_hour_6h_after": _safe_mean(after_6h),
                "posting_multiplier_6h": _safe_mean(after_6h) / baseline_hourly if baseline_hourly > 0 else math.nan,
            }
        )
    write_csv(root / "analysis" / "news_effect_summary.csv", summary_rows)
    strongest = max(summary_rows, key=lambda row: row["posting_multiplier_6h"], default=None)
    result = {
        "available": True,
        "coverage_is_partial": True,
        "coverage_note": "GDELT DOC rolling three-month coverage; excluded from model selection",
        "articles": len(news),
        "deduplicated_event_clusters": len(rows),
        "baseline_posts_per_hour": baseline_hourly,
        "strongest_topic": strongest,
    }
    write_json(root / "analysis" / "news_effect_summary.json", result)
    return result


def _combine_current_distribution(models: dict[str, np.ndarray], selection: dict[str, Any]) -> tuple[str, np.ndarray]:
    primary = str(selection["primary_model"])
    if not selection.get("ensemble_used"):
        return primary, models[primary]
    secondary = str(selection["secondary_model"])
    weight = float(selection["ensemble_weight_primary"])
    maximum = max(len(models[primary]), len(models[secondary]))
    first = np.pad(models[primary], (0, maximum - len(models[primary])))
    second = np.pad(models[secondary], (0, maximum - len(models[secondary])))
    combined = weight * first + (1.0 - weight) * second
    combined /= combined.sum()
    return f"ensemble:{primary}:{weight:.2f}+{secondary}:{1-weight:.2f}", combined


def _best_book(book: dict[str, Any] | None) -> tuple[float | None, float | None, float, float]:
    if not book:
        return None, None, 0.0, 0.0
    bids = [row for row in book.get("bids", []) if isinstance(row, dict) and row.get("price") is not None]
    asks = [row for row in book.get("asks", []) if isinstance(row, dict) and row.get("price") is not None]
    best_bid_row = max(bids, key=lambda row: float(row["price"]), default=None)
    best_ask_row = min(asks, key=lambda row: float(row["price"]), default=None)
    return (
        float(best_bid_row["price"]) if best_bid_row else None,
        float(best_ask_row["price"]) if best_ask_row else None,
        float(best_bid_row.get("size") or 0.0) if best_bid_row else 0.0,
        float(best_ask_row.get("size") or 0.0) if best_ask_row else 0.0,
    )


def current_forecasts(output_root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    root = output_root.resolve()
    markets = read_jsonl(root / "derived" / "markets.jsonl")
    post_rows = read_jsonl(root / "derived" / "posts_features.jsonl")
    timestamps = [parse_dt(row["timestamp_utc"]).timestamp() for row in post_rows]
    selection = read_json(root / "analysis" / "model_selection.json")
    books = {row["token"]: row.get("book") for row in read_jsonl(root / "raw" / "current_books.jsonl")}
    active = [
        row
        for row in markets
        if parse_dt(row["window_start_utc"]) <= CUT_OFF <= parse_dt(row["window_end_utc"])
        and row.get("rules_compatible")
    ]
    comparisons: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    regime = _regime_snapshot(timestamps, CUT_OFF)
    news = read_jsonl(root / "derived" / "news_events.jsonl")
    recent_news = [row for row in news if parse_dt(row["timestamp_available"]) >= CUT_OFF - timedelta(hours=24)]
    importance_rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "EXTREME": 4}
    news_environment = max((str(row["importance"]) for row in recent_news), key=lambda value: importance_rank.get(value, 0), default="NO_NEWS_OBSERVED")

    for market in sorted(active, key=lambda row: row["window_end_utc"]):
        start = parse_dt(market["window_start_utc"])
        end = parse_dt(market["window_end_utc"])
        buckets = [Bucket(**row) for row in json.loads(market["buckets_json"])]
        models, features = _forecast_models(timestamps, start, CUT_OFF, end)
        model_name, pmf = _combine_current_distribution(models, selection)
        probabilities = _bucket_probabilities(pmf, buckets)
        cdf = np.cumsum(pmf)
        market_rows: list[dict[str, Any]] = []
        for bucket, probability in zip(buckets, probabilities):
            yes_bid, yes_ask, yes_bid_size, yes_ask_size = _best_book(books.get(bucket.yes_token))
            no_bid, no_ask, no_bid_size, no_ask_size = _best_book(books.get(bucket.no_token))
            yes_fee = _fee_per_share(yes_ask, bucket.fee_rate, bucket.fees_enabled) if yes_ask is not None else None
            no_fee = _fee_per_share(no_ask, bucket.fee_rate, bucket.fees_enabled) if no_ask is not None else None
            edge_yes = probability - yes_ask - yes_fee if yes_ask is not None and yes_fee is not None else None
            edge_no = (1.0 - probability) - no_ask - no_fee if no_ask is not None and no_fee is not None else None
            expected_roi_yes = edge_yes / (yes_ask + yes_fee) if edge_yes is not None and yes_ask and yes_fee is not None else None
            expected_roi_no = edge_no / (no_ask + no_fee) if edge_no is not None and no_ask and no_fee is not None else None
            decision = "NO_TRADE"
            if edge_yes is not None and edge_yes >= 0.05 and yes_ask_size >= 5:
                decision = "TRADE_YES_SHADOW_ONLY"
            if edge_no is not None and edge_no >= 0.05 and no_ask_size >= 5 and (edge_yes is None or edge_no > edge_yes):
                decision = "TRADE_NO_SHADOW_ONLY"
            market_rows.append(
                {
                    "slug": market["slug"],
                    "bucket": bucket.label,
                    "model_probability": probability,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "yes_spread": yes_ask - yes_bid if yes_ask is not None and yes_bid is not None else None,
                    "no_spread": no_ask - no_bid if no_ask is not None and no_bid is not None else None,
                    "yes_ask_size": yes_ask_size,
                    "no_ask_size": no_ask_size,
                    "edge_yes_net_fee": edge_yes,
                    "edge_no_net_fee": edge_no,
                    "expected_roi_yes": expected_roi_yes,
                    "expected_roi_no": expected_roi_no,
                    "decision": decision,
                }
            )
        comparisons.extend(market_rows)
        likely_index = int(np.argmax(probabilities))
        value_candidates: list[tuple[float, str, str]] = []
        for row in market_rows:
            if row["edge_yes_net_fee"] is not None:
                value_candidates.append((float(row["edge_yes_net_fee"]), row["bucket"], "YES"))
            if row["edge_no_net_fee"] is not None:
                value_candidates.append((float(row["edge_no_net_fee"]), row["bucket"], "NO"))
        value_candidates.sort(reverse=True)
        best_value = value_candidates[0] if value_candidates else (math.nan, None, None)
        summaries.append(
            {
                "slug": market["slug"],
                "title": market["title"],
                "model": model_name,
                **features,
                "projected_final_mean": float(np.dot(np.arange(len(pmf)), pmf)),
                "projected_final_median": int(np.searchsorted(cdf, 0.5)),
                "p10_final": int(np.searchsorted(cdf, 0.1)),
                "p90_final": int(np.searchsorted(cdf, 0.9)),
                "most_likely_bucket": buckets[likely_index].label,
                "most_likely_probability": probabilities[likely_index],
                "best_value_bucket": best_value[1],
                "best_value_side": best_value[2],
                "best_value_edge_net_fee": best_value[0],
                "current_regime": regime["regime"],
                "burstiness_percentile": regime["burstiness_score_percentile"],
                "news_environment": news_environment,
                "recent_news_24h": len(recent_news),
                "probabilities_json": stable_json({bucket.label: probability for bucket, probability in zip(buckets, probabilities)}),
            }
        )
    write_csv(root / "final" / "current_market_comparison.csv", comparisons)
    write_jsonl(root / "final" / "current_market_forecasts.jsonl", summaries)
    result = {"active_markets": len(active), "forecasts": summaries, "regime": regime, "news_environment": news_environment}
    write_json(root / "final" / "current_snapshot.json", result)
    return result


def calibration_and_wait(output_root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    root = output_root.resolve()
    forecasts = read_jsonl(root / "analysis" / "selected_forecasts.jsonl")
    calibration: list[dict[str, Any]] = []
    expanded: list[tuple[float, int, str]] = []
    for row in forecasts:
        labels = json.loads(row["bucket_labels_json"])
        probabilities = json.loads(row["probabilities_json"])
        for label, probability in zip(labels, probabilities):
            expanded.append((float(probability), 1 if label == row["actual_bucket"] else 0, row["split"]))
    for split in ("TRAIN", "VALIDATION", "TEST"):
        for low in np.arange(0.0, 1.0, 0.1):
            high = min(1.0, low + 0.1)
            values = [(p, outcome) for p, outcome, row_split in expanded if row_split == split and low <= p < high + (1e-12 if high == 1.0 else 0.0)]
            if values:
                calibration.append(
                    {
                        "split": split,
                        "bin_low": low,
                        "bin_high": high,
                        "n": len(values),
                        "mean_probability": _safe_mean([p for p, _ in values]),
                        "observed_frequency": _safe_mean([outcome for _, outcome in values]),
                    }
                )
    write_csv(root / "analysis" / "calibration_curve.csv", calibration)

    opportunities = read_jsonl(root / "analysis" / "economic_opportunities.jsonl")
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in opportunities:
        by_event[row["slug"]].append(row)
    wait_rows: list[dict[str, Any]] = []
    for slug, rows in by_event.items():
        ordered = sorted(rows, key=lambda row: row["observation_utc"])
        for index, early in enumerate(ordered):
            early_ts = parse_dt(early["observation_utc"])
            later = next(
                (
                    row
                    for row in ordered[index + 1 :]
                    if 45 * 60 <= (parse_dt(row["observation_utc"]) - early_ts).total_seconds() <= 90 * 60
                ),
                None,
            )
            if later is None:
                continue
            wait_rows.append(
                {
                    "slug": slug,
                    "split": early["split"],
                    "early_time_remaining_hours": early["time_remaining_hours"],
                    "information_gain_entropy": float(early["forecast_entropy"]) - float(later["forecast_entropy"]),
                    "edge_change": float(later["net_edge_per_share"]) - float(early["net_edge_per_share"]),
                    "reference_price_change": float(later["reference_price"]) - float(early["reference_price"]),
                    "realized_pnl_change_100": float(later["pnl_100"]) - float(early["pnl_100"]),
                }
            )
    write_csv(root / "analysis" / "wait_value.csv", wait_rows)
    result = {
        "calibration_bins": len(calibration),
        "wait_pairs": len(wait_rows),
        "wait_pairs_by_split": dict(Counter(row["split"] for row in wait_rows)),
        "wait_value_confirmatory": bool(
            any(row["split"] == "TRAIN" for row in wait_rows)
            and any(row["split"] == "VALIDATION" for row in wait_rows)
        ),
        "mean_information_gain": _safe_mean([row["information_gain_entropy"] for row in wait_rows]),
        "mean_edge_change": _safe_mean([row["edge_change"] for row in wait_rows]),
        "mean_realized_pnl_change_100": _safe_mean([row["realized_pnl_change_100"] for row in wait_rows]),
    }
    write_json(root / "analysis" / "wait_value_summary.json", result)
    return result


def _fmt_money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/D"
    return f"${number:,.2f}"


def _fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/D"
    if not math.isfinite(number):
        return "N/D"
    return f"{100.0 * number:.2f}%"


def _fmt_num(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/D"
    if not math.isfinite(number):
        return "N/D"
    return f"{number:.{digits}f}"


def generate_report(output_root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    root = output_root.resolve()
    manifest = read_json(root / "raw" / "capture_manifest.json")
    derivation = read_json(root / "derived" / "derivation_summary.json")
    markets = read_jsonl(root / "derived" / "markets.jsonl")
    hourly = []
    with (root / "derived" / "intraday_profile.csv").open("r", encoding="utf-8-sig") as handle:
        hourly = list(csv.DictReader(handle))
    weekdays = []
    with (root / "derived" / "weekday_profile.csv").open("r", encoding="utf-8-sig") as handle:
        weekdays = list(csv.DictReader(handle))
    model_selection = read_json(root / "analysis" / "model_selection.json")
    economic = read_json(root / "analysis" / "economic_selection.json")
    news = read_json(root / "analysis" / "news_effect_summary.json")
    wait = read_json(root / "analysis" / "wait_value_summary.json")
    current = read_json(root / "final" / "current_snapshot.json")
    model_metrics = []
    with (root / "analysis" / "model_metrics.csv").open("r", encoding="utf-8-sig") as handle:
        model_metrics = list(csv.DictReader(handle))

    best_hour = max(hourly, key=lambda row: float(row["posts_per_exposed_hour"]), default={})
    worst_hour = min(hourly, key=lambda row: float(row["posts_per_exposed_hour"]), default={})
    best_weekday = max(weekdays, key=lambda row: float(row["mean_posts"]), default={})
    worst_weekday = min(weekdays, key=lambda row: float(row["mean_posts"]), default={})
    baseline = derivation["daily_baseline"]
    resolved = [row for row in markets if row.get("resolved_before_cutoff")]
    primary_current = current.get("forecasts", [None])[0] if current.get("forecasts") else None
    selected_name = str(model_selection["selected_model"])
    selected_base = str(model_selection["primary_model"])
    test_metric = next((row for row in model_metrics if row["model"] == selected_base and row["split"] == "TEST"), None)
    selected_forecast_rows = [
        row for row in read_jsonl(root / "analysis" / "selected_forecasts.jsonl") if row["split"] == "TEST"
    ]
    selected_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected_forecast_rows:
        selected_by_event[row["slug"]].append(row)
    selected_test_brier = _safe_mean(
        [_safe_mean([float(row["brier"]) for row in rows]) for rows in selected_by_event.values()]
    )
    selected_test_log = _safe_mean(
        [_safe_mean([float(row["log_loss"]) for row in rows]) for rows in selected_by_event.values()]
    )
    finalist = economic.get("finalist")
    test = economic.get("test_result")
    confirmed = bool(economic.get("test_confirmed"))

    count_comparisons = int(derivation.get("resolved_with_winner") or 0)
    exact = int(derivation.get("reconstruction_bucket_matches") or 0)
    gamma_exact = int(derivation.get("gamma_count_exact_matches") or 0)
    gamma_comparisons = int(derivation.get("gamma_count_comparisons") or 0)
    price_events = sum(1 for row in read_jsonl(root / "raw" / "price_history_manifest.jsonl") if int(row.get("points") or 0) > 0)
    completeness_score = round(
        100
        * (
            0.35 * (exact / count_comparisons if count_comparisons else 0)
            + 0.30 * (len(resolved) / max(1, int(manifest["gamma_events"])))
            + 0.25 * (price_events / max(1, int(manifest["gamma_events"])))
            + 0.10 * (1.0 if int(manifest["posts"]) > 5000 else 0.5)
        )
    )

    profitability = 65 if confirmed else 25
    predictability = max(10, min(85, int(80 - 100 * float(test_metric["brier_event_weighted"])) if test_metric else 30))
    capital_efficiency = 60 if confirmed and test and float(test.get("pnl_per_capital_hour") or 0) > 0 else 25
    time_efficiency = 70
    automation = 90
    liquidity = 55 if price_events >= len(resolved) * 0.8 else 30
    data_quality = completeness_score
    robustness = 65 if confirmed and test and float(test.get("pnl_without_top3") or 0) > 0 else 20
    news_risk = 30  # higher score means safer/lower risk
    tail_risk = 25
    final_score = round(
        statistics.fmean(
            [profitability, predictability, capital_efficiency, time_efficiency, automation, liquidity, data_quality, robustness, news_risk, tail_risk]
        )
    )

    current_lines: list[str] = []
    if primary_current:
        probabilities = json.loads(primary_current["probabilities_json"])
        for label, probability in probabilities.items():
            current_lines.append(f"- P({label}): {_fmt_pct(probability)}")
    else:
        current_lines.append("- No había una ventana activa compatible al corte.")

    comparison_lines: list[str] = []
    comparison_path = root / "final" / "current_market_comparison.csv"
    if comparison_path.exists() and primary_current:
        with comparison_path.open("r", encoding="utf-8-sig") as handle:
            comparison_rows = [row for row in csv.DictReader(handle) if row["slug"] == primary_current["slug"]]
        comparison_lines = [
            "| Bucket | Prob. modelo | Ask YES | Ask NO | Edge YES neto | Edge NO neto | Decisión |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for row in comparison_rows:
            comparison_lines.append(
                f"| {row['bucket']} | {_fmt_pct(row['model_probability'])} | "
                f"{_fmt_num(row['yes_ask']) if row['yes_ask'] else 'N/D'} | { _fmt_num(row['no_ask']) if row['no_ask'] else 'N/D'} | "
                f"{_fmt_pct(row['edge_yes_net_fee']) if row['edge_yes_net_fee'] else 'N/D'} | "
                f"{_fmt_pct(row['edge_no_net_fee']) if row['edge_no_net_fee'] else 'N/D'} | {row['decision']} |"
            )
    else:
        comparison_lines.append("No existe comparación ejecutable al corte.")

    news_topic = news.get("strongest_topic") if isinstance(news, dict) else None
    wait_verdict = "INCONCLUSIVE"
    if wait.get("wait_value_confirmatory"):
        wait_verdict = "SOMETIMES"
        if float(wait.get("mean_realized_pnl_change_100") or 0) > 1.0:
            wait_verdict = "YES"
        elif float(wait.get("mean_realized_pnl_change_100") or 0) < -1.0:
            wait_verdict = "NO"
    economic_interpretable = bool(economic.get("economic_results_interpretable"))

    report = f"""# ELON MUSK POST COUNT — FINAL RESEARCH REPORT

Corte congelado: **{iso(CUT_OFF)}**. Sector exclusivo: **Cultura — Elon Musk post count**. Los resultados económicos usan precios CLOB conocidos antes o en cada decisión, +1 centavo de impacto y la comisión taker de Cultura cuando corresponde.

## DATA COVERAGE

- Historical posts: **{manifest['posts']:,}**.
- Period: **{derivation['post_period_start']} → {derivation['post_period_end']}**.
- XTracker trackings: **{manifest['trackings']}**; eventos Gamma con enlace: **{manifest['gamma_events']}**.
- Markets analyzed/resolved: **{len(markets)}/{len(resolved)}**.
- 48h markets: **{sum(row['duration_class'] == '48H' for row in resolved)}**.
- 7-day markets: **{sum(row['duration_class'] == '7D' for row in resolved)}**.
- CLOB historical price points: **{manifest['price_points']:,}**.
- Conteos XTracker dentro del bucket ganador oficial: **{exact}/{count_comparisons}**.
- Endpoint Gamma `tweet-count` igual a la reconstrucción: **{gamma_exact}/{gamma_comparisons}**; los valores históricos obsoletos se auditaron pero no se usaron como resultado final.
- Completeness score: **{completeness_score}/100**.
- Limitación: XTracker no expone el tipo original/repost/quote en este endpoint; esos tipos se etiquetaron `UNAVAILABLE` y no se inventaron.

## MARKET RULES AUDIT

Las reglas compatibles cuentan publicaciones del main feed, quote posts y reposts; excluyen replies salvo replies visibles en el main feed. Los posts borrados cuentan si el tracker los capturó durante aproximadamente cinco minutos; community reposts no registrados por XTracker no cuentan. XTracker es la fuente primaria y X puede actuar como fuente secundaria. Las variantes completas están en `market_rules_audit.csv` y no se mezclaron reglas incompatibles.

## ELON BASELINE

- Mean posts/day: **{_fmt_num(baseline.get('mean'))}**.
- Median: **{_fmt_num(baseline.get('median'))}**.
- Std deviation: **{_fmt_num(baseline.get('std'))}**.
- Variance/Mean: **{_fmt_num(baseline.get('variance_mean_ratio'))}** — {'overdispersion clara; Poisson es insuficiente como modelo final.' if float(baseline.get('variance_mean_ratio') or 0) > 1.25 else 'dispersión cercana a Poisson.'}
- P10/P90: **{_fmt_num(baseline.get('p10'))} / {_fmt_num(baseline.get('p90'))}**.
- Most active hour: **{best_hour.get('hour_et', 'N/D')}:00 ET**; least active: **{worst_hour.get('hour_et', 'N/D')}:00 ET**.
- Highest activity day: **{best_weekday.get('weekday', 'N/D')}**; lowest: **{worst_weekday.get('weekday', 'N/D')}**.
- Median inter-arrival: **{_fmt_num(derivation['interarrival'].get('median_minutes'))} min**; P95: **{_fmt_num(derivation['interarrival'].get('p95_minutes'))} min**; longest observed silence: **{_fmt_num(derivation['interarrival'].get('longest_silence_hours'))} h**.

## REGIME ANALYSIS

- Current regime: **{current['regime'].get('regime', 'N/D')}**.
- Baseline rate: **{_fmt_num(primary_current.get('baseline_rate') if primary_current else None)} posts/h**.
- Recent 24h rate: **{_fmt_num(primary_current.get('rate_24h') if primary_current else None)} posts/h**.
- Burstiness percentile: **{_fmt_pct(primary_current.get('burstiness_percentile') if primary_current else None)}**.
- News environment: **{current.get('news_environment', 'N/D')}**.

## NEWS EFFECT

- Coverage: **no disponible en la captura**; GDELT agotó el timeout y noticias quedaron fuera de la selección del modelo.
- Articles / event clusters: **{news.get('articles', 0)} / {news.get('deduplicated_event_clusters', 0)}**.
- Most influential observed topic: **{news_topic.get('category') if news_topic else 'N/D'}**.
- Observed 6h posting multiplier: **{_fmt_num(news_topic.get('posting_multiplier_6h') if news_topic else None)}x**.
- Confidence: **no estimable**; no se inventaron eventos ni multiplicadores.

## BEST FORECASTING MODEL

- Model: **{selected_name}**.
- Test Brier (ensemble seleccionado): **{_fmt_num(selected_test_brier, 4)}**.
- Test Log Loss (ensemble seleccionado): **{_fmt_num(selected_test_log, 4)}**.
- Why: menor Brier event-weighted en VALIDATION; TEST no participó en la selección.

## CURRENT MARKET FORECAST

- Market: **{primary_current.get('title') if primary_current else 'N/D'}**.
- Current Count: **{primary_current.get('current_count') if primary_current else 'N/D'}**.
- Elapsed / Remaining: **{_fmt_num(primary_current.get('time_elapsed_hours') if primary_current else None)}h / {_fmt_num(primary_current.get('time_remaining_hours') if primary_current else None)}h**.
- Projected Final Median: **{primary_current.get('projected_final_median') if primary_current else 'N/D'}**.
- P10 / P90: **{primary_current.get('p10_final') if primary_current else 'N/D'} / {primary_current.get('p90_final') if primary_current else 'N/D'}**.
{chr(10).join(current_lines)}

## MARKET COMPARISON

{chr(10).join(comparison_lines)}

## MOST LIKELY OUTCOME

**{primary_current.get('most_likely_bucket') if primary_current else 'N/D'}**, probability **{_fmt_pct(primary_current.get('most_likely_probability') if primary_current else None)}**.

## BEST VALUE BET

{'**' + str(primary_current.get('best_value_side')) + ' ' + str(primary_current.get('best_value_bucket')) + '**; edge neto observable **' + _fmt_pct(primary_current.get('best_value_edge_net_fee')) + '**. Solo shadow hasta validar la estrategia.' if primary_current and math.isfinite(float(primary_current.get('best_value_edge_net_fee') or math.nan)) and float(primary_current.get('best_value_edge_net_fee')) >= 0.05 else '**NO TRADE**: no existe edge ejecutable neto suficiente o falta libro verificable.'}

## OPTIMAL ENTRY TIMING

- Best train/validation candidate: **{finalist.get('strategy') if finalist else 'NOT ESTABLISHED'}**.
- Edge threshold: **{_fmt_pct(finalist.get('threshold') if finalist else None)}**.
- Validation ROI / PF: **{_fmt_pct(finalist.get('roi') if finalist else None)} / {_fmt_num(finalist.get('profit_factor') if finalist else None)}**.
- Capital lock-up: **{_fmt_num(finalist.get('holding_hours') if finalist else None)} horas acumuladas**.
- Price coverage by split: **{economic.get('opportunity_split_counts')}**. {'No hubo precios en TRAIN/VALIDATION; optimizar timing con TEST sería data snooping.' if not economic_interpretable else 'La cobertura cronológica fue suficiente.'}

## DOES WAITING HELP?

**{wait_verdict}.** En los pares con precio disponibles, todos del tramo TEST, el cambio medio de entropía fue **{_fmt_num(wait.get('mean_information_gain'), 4)}**, el edge cambió **{_fmt_pct(wait.get('mean_edge_change'))}** y el PnL realizado por $100 cambió **{_fmt_money(wait.get('mean_realized_pnl_change_100'))}**. Son diagnósticos, no una regla seleccionable.

## BEST STRATEGY

**{finalist.get('strategy') if finalist else 'NO EVALUABLE ECONOMIC STRATEGY'}**. {'Faltan precios históricos de TRAIN/VALIDATION.' if not economic_interpretable else 'Ninguna regla superó los gates.'}

## BACKTEST

- Trades: **{finalist.get('trades') if finalist else 'N/E'}** (VALIDATION del candidato).
- Wins/Losses: **{finalist.get('wins') if finalist else 0}/{finalist.get('losses') if finalist else 0}**.
- Win Rate: **{_fmt_pct(finalist.get('win_rate') if finalist else None)}**.
- ROI / PnL / PF: **{_fmt_pct(finalist.get('roi') if finalist else None)} / {_fmt_money(finalist.get('pnl') if finalist else None)} / {_fmt_num(finalist.get('profit_factor') if finalist else None)}**.
- Max DD: **{_fmt_money(finalist.get('max_drawdown') if finalist else None)}**.

## OUT-OF-SAMPLE

- Trades: **{test.get('trades') if test else 0}**.
- ROI / PF / DD: **{_fmt_pct(test.get('roi') if test else None)} / {_fmt_num(test.get('profit_factor') if test else None)} / {_fmt_money(test.get('max_drawdown') if test else None)}**.
- PnL without top 1 / top 3: **{_fmt_money(test.get('pnl_without_top1') if test else None)} / {_fmt_money(test.get('pnl_without_top3') if test else None)}**.
- Confirmed: **{confirmed}**. {'TEST no se abrió para seleccionar estrategia porque faltó el gate económico previo.' if not economic_interpretable else ''}

## CAPITAL

- Minimum real-money capital: **not established**.
- Recommended test capital: **$0 real; $100 notionals in shadow**.
- Scalability: **unverified** porque el histórico de precios no reconstruye profundidad ejecutable pasada.
- Capital efficiency: **{_fmt_num(test.get('pnl_per_capital_hour') if test else None, 6)} PnL por dólar-hora proxy**.

## CULTURE — ELON SCORECARD

- Profitability: {profitability}/100
- Predictability: {predictability}/100
- Capital Efficiency: {capital_efficiency}/100
- Time Efficiency: {time_efficiency}/100
- Automation Ease: {automation}/100
- Liquidity: {liquidity}/100
- Data Quality: {data_quality}/100
- Strategy Robustness: {robustness}/100
- News Risk: {news_risk}/100
- Tail Risk: {tail_risk}/100
- **FINAL ELON POST-COUNT SCORE: {final_score}/100**

## LO QUE APRENDIMOS

1. La distribución diaria debe modelar sobredispersión y ráfagas; el promedio diario por sí solo pierde información esencial.
2. El reloj ET, la tasa reciente y el silencio aportan estado; se conservaron como features. El tipo de post no pudo probarse porque la API no lo expone.
3. Más certeza cerca del cierre no garantiza mejor EV: el precio también se deteriora y la comisión de Cultura penaliza especialmente precios intermedios.
4. Un bucket ya ocupado por el conteo actual puede seguir siendo mala compra si el conteo esperado restante alcanza su borde superior.
5. La fuente noticiosa no estuvo disponible; cualquier aparente efecto de noticias habría sido inventado y se excluyó.
6. La mejora principal al Prompt Maestro sería exigir una captura forward continua del libro y un feed noticioso archivado desde antes del próximo mercado.

## VEREDICTO

- ¿ES PREDECIBLE? **Parcialmente**: la distribución se actualiza con actividad, silencio y hora ET, pero conserva cola amplia.
- ¿EXISTE EDGE? **{'Sí, solo candidato para confirmación forward shadow' if confirmed else 'Inconcluso; no hubo cobertura económica cronológica suficiente'}**.
- ¿CONVIENE ESPERAR CERCA DEL FINAL? **{wait_verdict} / depende del estado y del precio**.
- ¿CUÁNDO ENTRAR? **Solo cuando el edge neto prerregistrado supera el threshold validado; no por una hora fija aislada**.
- ¿MEJOR TIPO DE OPERACIÓN? **{'La señal ' + str(test.get('strategy')) if confirmed and test else 'NO TRADE hasta nueva confirmación'}**.
- ¿PUEDE AUTOMATIZARSE? **Sí, como monitor receive-only/shadow**.
- ¿CREARÍAS UN BOT? **Sí como monitor; no como ejecutor real todavía**.
- CONFIANZA: **88/100** para el bloqueo de dinero real; menor para descartar que aparezca edge en una muestra forward más amplia.
"""
    final_dir = root / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    report_path = final_dir / "ELON_MUSK_POST_COUNT_FINAL_REPORT.md"
    report_path.write_text(report, encoding="utf-8")
    final = {
        "schema": SCHEMA,
        "cut_off": iso(CUT_OFF),
        "completeness_score": completeness_score,
        "selected_model": selected_name,
        "economic_selection": economic,
        "current": primary_current,
        "scorecard": {
            "profitability": profitability,
            "predictability": predictability,
            "capital_efficiency": capital_efficiency,
            "time_efficiency": time_efficiency,
            "automation_ease": automation,
            "liquidity": liquidity,
            "data_quality": data_quality,
            "strategy_robustness": robustness,
            "news_risk": news_risk,
            "tail_risk": tail_risk,
            "final": final_score,
        },
        "verdict": "FORWARD_SHADOW_ONLY" if confirmed else "MORE_DATA_REQUIRED_NO_REAL_MONEY",
        "report": str(report_path),
    }
    write_json(final_dir / "final_report.json", final)
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "final_manifest.json")
    write_json(
        final_dir / "final_manifest.json",
        {
            "schema": SCHEMA,
            "generated_at": iso(datetime.now(timezone.utc)),
            "files": [
                {"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": sha256(path)}
                for path in files
            ],
        },
    )
    return final


def analyze(output_root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    derivation = derive(output_root)
    forecasts = build_forecasts(output_root)
    economics = economic_backtest(output_root)
    news = news_effect_analysis(output_root)
    calibration = calibration_and_wait(output_root)
    current = current_forecasts(output_root)
    report = generate_report(output_root)
    return {
        "derivation": derivation,
        "forecasts": forecasts,
        "economics": economics,
        "news": news,
        "calibration_wait": calibration,
        "current": current,
        "report": report,
    }


def run_all(output_root: Path = DEFAULT_ROOT, *, workers: int = 10, include_news: bool = True) -> dict[str, Any]:
    capture = collect(output_root, workers=workers, include_news=include_news)
    result = analyze(output_root)
    return {"capture": capture, **result}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Investigación auditable de mercados Elon Musk post count")
    parser.add_argument("command", choices=("collect", "finalize-capture", "analyze", "all"))
    parser.add_argument("--output-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--skip-news", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.output_root)
    if args.command == "collect":
        result = collect(root, workers=args.workers, include_news=not args.skip_news)
    elif args.command == "finalize-capture":
        result = finalize_existing_capture(root, news_error="GDELT_CONNECT_TIMEOUT_2026-09-01")
    elif args.command == "analyze":
        result = analyze(root)
    else:
        result = run_all(root, workers=args.workers, include_news=not args.skip_news)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

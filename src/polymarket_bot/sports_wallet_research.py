from __future__ import annotations

import csv
import concurrent.futures
import hashlib
import json
import math
import os
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
SCHEMA = "sports_wallet_research_v001"
CUTOFF_INCLUSIVE_UNIX = 1_788_191_183
CUTOFF_UTC = "2026-08-31T15:46:23+00:00"

TRADERS: tuple[dict[str, str], ...] = (
    {
        "key": "flaznorp",
        "requested_name": "Flaznorp",
        "profile_name": "Flaznorp",
        "proxy_wallet": "0x821dab0565ebf5b327f51db06223fdcfe01acf16",
        "profile_url": "https://polymarket.com/@flaznorp",
    },
    {
        "key": "trader_b",
        "requested_name": "Trader B",
        "profile_name": "0x5016c48436AB3eFA2Ab54b117d0C08fa1a4a1eEB-1778328420816",
        "proxy_wallet": "0x5016c48436ab3efa2ab54b117d0c08fa1a4a1eeb",
        "profile_url": "https://polymarket.com/@0x5016c48436ab3efa2ab54b117d0c08fa1a4a1eeb-1778328420816",
    },
    {
        "key": "nigiri99",
        "requested_name": "nigiri99",
        "profile_name": "nigiri99",
        "proxy_wallet": "0xdc41c39b95453c943174f369926018f6963bdd7e",
        "profile_url": "https://polymarket.com/@nigiri99",
    },
    {
        "key": "kulijan",
        "requested_name": "kulijan",
        "profile_name": "Kulijan",
        "proxy_wallet": "0x66834eefe81eb46b184eda12a4a8720bf809f5f4",
        "profile_url": "https://polymarket.com/@kulijan",
    },
)

ACTIVITY_TYPES = (
    "TRADE",
    "SPLIT",
    "MERGE",
    "REDEEM",
    "REWARD",
    "CONVERSION",
    "MAKER_REBATE",
    "TAKER_REBATE",
    "REFERRAL_REWARD",
)

ESPORTS_MARKERS = (
    "esport",
    "league of legends",
    "dota",
    "counter-strike",
    "counter strike",
    "cs2",
    "valorant",
    "call of duty",
    "rainbow six",
    "overwatch",
    "starcraft",
    "rocket league",
    "mobile legends",
    "arena of valor",
)


class SportsResearchError(RuntimeError):
    """Raised when evidence cannot be captured or verified without assumptions."""


@dataclass(frozen=True)
class PageAudit:
    endpoint: str
    start: int | None
    end_inclusive: int | None
    offset: int
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "start": self.start,
            "end_inclusive": self.end_inclusive,
            "offset": self.offset,
            "count": self.count,
        }


def utc_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_digest(row: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json(row).encode("utf-8")).hexdigest()


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SportsResearchError(f"Campo numérico inválido: {field}") from exc
    if not math.isfinite(result):
        raise SportsResearchError(f"Campo no finito: {field}")
    return result


def _url(base: str, path: str, params: dict[str, Any] | None = None) -> str:
    if not params:
        return f"{base}{path}"
    normalized: list[tuple[str, Any]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            normalized.append((key, ",".join(str(item) for item in value)))
        else:
            normalized.append((key, value))
    return f"{base}{path}?{urllib.parse.urlencode(normalized)}"


class PublicApiClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        retries: int = 10,
        user_agent: str = "polymarker-quantbot-sports-research/0.1",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.user_agent = user_agent
        self.requests = 0
        self._request_lock = threading.Lock()

    def get_json(self, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
            )
            try:
                with self._request_lock:
                    self.requests += 1
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    raw = response.read().decode("utf-8")
                return json.loads(raw)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                    raise SportsResearchError(f"HTTP {exc.code}: {url}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt + 1 < self.retries:
                time.sleep(min(8.0, 0.5 * (2**attempt)))
        raise SportsResearchError(f"No se pudo consultar {url}: {last_error}")


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    if partial.exists():
        partial.unlink()
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    partial.replace(path)


def write_json(path: Path, payload: Any) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    count = 0
    with partial.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(stable_json(row) + "\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)
    return count


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise SportsResearchError(f"Fila JSONL inválida en {path}:{line_number}")
            yield value


def verify_identity(client: PublicApiClient, spec: dict[str, str]) -> dict[str, Any]:
    search = client.get_json(
        _url(
            GAMMA_API,
            "/public-search",
            {
                "q": spec["profile_name"],
                "search_profiles": "true",
                "limit_per_type": 50,
            },
        )
    )
    profiles = search.get("profiles") if isinstance(search, dict) else None
    matches = [
        row
        for row in (profiles or [])
        if isinstance(row, dict)
        and str(row.get("name") or "").casefold() == spec["profile_name"].casefold()
        and str(row.get("proxyWallet") or "").lower() == spec["proxy_wallet"]
    ]
    if len(matches) != 1:
        raise SportsResearchError(
            f"{spec['key']}: búsqueda oficial no produjo una identidad exacta única ({len(matches)})"
        )
    profile_url = _url(
        GAMMA_API, "/public-profile", {"address": spec["proxy_wallet"]}
    )
    profile = client.get_json(profile_url)
    if not isinstance(profile, dict):
        raise SportsResearchError(f"{spec['key']}: public-profile inválido")
    if str(profile.get("proxyWallet") or "").lower() != spec["proxy_wallet"]:
        raise SportsResearchError(f"{spec['key']}: public-profile no confirma wallet")
    if str(profile.get("name") or "").casefold() != spec["profile_name"].casefold():
        raise SportsResearchError(f"{spec['key']}: public-profile no confirma nombre")
    return {
        "key": spec["key"],
        "requested_name": spec["requested_name"],
        "profile_name": profile.get("name"),
        "proxy_wallet": str(profile["proxyWallet"]).lower(),
        "pseudonym": profile.get("pseudonym"),
        "created_at": profile.get("createdAt"),
        "verified_badge": bool(profile.get("verifiedBadge", False)),
        "profile_url": spec["profile_url"],
        "source_urls": {
            "search": _url(
                GAMMA_API,
                "/public-search",
                {
                    "q": spec["profile_name"],
                    "search_profiles": "true",
                    "limit_per_type": 50,
                },
            ),
            "public_profile": profile_url,
        },
    }


def find_activity_bounds(
    client: PublicApiClient, wallet: str, cutoff_inclusive: int
) -> tuple[int | None, int | None]:
    common = {"user": wallet, "type": "TRADE", "limit": 1, "sortBy": "TIMESTAMP"}
    first_page = client.get_json(
        _url(DATA_API, "/activity", {**common, "sortDirection": "ASC", "end": cutoff_inclusive})
    )
    last_page = client.get_json(
        _url(DATA_API, "/activity", {**common, "sortDirection": "DESC", "end": cutoff_inclusive})
    )
    if not isinstance(first_page, list) or not isinstance(last_page, list):
        raise SportsResearchError("Respuesta inválida al buscar límites de actividad")
    first = int(first_page[0]["timestamp"]) if first_page else None
    last = int(last_page[0]["timestamp"]) if last_page else None
    if (first is None) != (last is None):
        raise SportsResearchError("Límites de actividad inconsistentes")
    if first is not None and not (first <= last <= cutoff_inclusive):
        raise SportsResearchError("Límites de actividad fuera del corte")
    return first, last


def _fetch_activity_window(
    client: PublicApiClient,
    wallet: str,
    start: int,
    end_exclusive: int,
    *,
    activity_types: Sequence[str],
    page_limit: int = 500,
    max_offset: int = 5000,
) -> tuple[list[dict[str, Any]], list[PageAudit], int]:
    if end_exclusive <= start:
        return [], [], 0
    rows: list[dict[str, Any]] = []
    pages: list[PageAudit] = []
    offset = 0
    while True:
        url = _url(
            DATA_API,
            "/activity",
            {
                "user": wallet,
                "type": activity_types,
                "start": start,
                "end": end_exclusive - 1,
                "sortBy": "TIMESTAMP",
                "sortDirection": "ASC",
                "limit": page_limit,
                "offset": offset,
            },
        )
        page = client.get_json(url)
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise SportsResearchError("Página de activity inválida")
        for row in page:
            timestamp = int(row.get("timestamp", -1))
            if not start <= timestamp < end_exclusive:
                raise SportsResearchError("Activity devolvió una fila fuera de ventana")
            if str(row.get("proxyWallet") or "").lower() != wallet.lower():
                raise SportsResearchError("Activity devolvió una fila de otra wallet")
            if str(row.get("type") or "") not in activity_types:
                raise SportsResearchError("Activity devolvió un tipo no solicitado")
        pages.append(PageAudit("activity", start, end_exclusive - 1, offset, len(page)))
        rows.extend(page)
        if len(page) < page_limit:
            return rows, pages, 0
        if offset == max_offset:
            if end_exclusive - start <= 1:
                raise SportsResearchError(
                    f"Más de {max_offset + page_limit} filas comparten el segundo {start}"
                )
            midpoint = start + (end_exclusive - start) // 2
            left, left_pages, left_discarded = _fetch_activity_window(
                client,
                wallet,
                start,
                midpoint,
                activity_types=activity_types,
                page_limit=page_limit,
                max_offset=max_offset,
            )
            right, right_pages, right_discarded = _fetch_activity_window(
                client,
                wallet,
                midpoint,
                end_exclusive,
                activity_types=activity_types,
                page_limit=page_limit,
                max_offset=max_offset,
            )
            return (
                left + right,
                left_pages + right_pages,
                len(rows) + left_discarded + right_discarded,
            )
        offset += page_limit


def capture_activity(
    client: PublicApiClient,
    wallet: str,
    start: int,
    cutoff_inclusive: int,
    output_path: Path,
    *,
    activity_types: Sequence[str] = ACTIVITY_TYPES,
    initial_window_seconds: int = 86_400,
    window_workers: int = 4,
) -> dict[str, Any]:
    if output_path.exists():
        exact: set[str] = set()
        duplicates = 0
        count = 0
        min_timestamp: int | None = None
        max_timestamp: int | None = None
        for row in read_jsonl(output_path):
            if str(row.get("proxyWallet") or "").lower() != wallet.lower():
                raise SportsResearchError("Archivo de activity contiene otra wallet")
            timestamp = int(row.get("timestamp", -1))
            if not start <= timestamp <= cutoff_inclusive:
                raise SportsResearchError("Archivo de activity está fuera del corte")
            digest = row_digest(row)
            if digest in exact:
                duplicates += 1
            exact.add(digest)
            count += 1
            min_timestamp = timestamp if min_timestamp is None else min(min_timestamp, timestamp)
            max_timestamp = timestamp if max_timestamp is None else max(max_timestamp, timestamp)
        return {
            "capture_status": "VERIFIED_EXISTING",
            "canonical_api_records": count,
            "processed_records": count,
            "exact_duplicate_records": duplicates,
            "unique_exact_records": len(exact),
            "min_timestamp": min_timestamp,
            "max_timestamp": max_timestamp,
            "discarded_probe_records_due_to_adaptive_split": None,
            "canonical_pages": [],
            "canonical_page_count": None,
            "sha256": sha256_file(output_path),
            "note": "El archivo se verificó localmente; el detalle de páginas pertenece a la primera captura interrumpida antes del manifiesto.",
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_suffix(output_path.suffix + ".partial")
    page_audits: list[PageAudit] = []
    discarded_probe_records = 0
    canonical_rows = 0
    seen_exact: set[str] = set()
    exact_duplicates = 0
    min_timestamp: int | None = None
    max_timestamp: int | None = None
    end_exclusive = cutoff_inclusive + 1
    windows = [
        (window_start, min(end_exclusive, window_start + initial_window_seconds))
        for window_start in range(start, end_exclusive, initial_window_seconds)
    ]
    checkpoint_root = output_path.parent / "checkpoints" / output_path.stem
    checkpoint_root.mkdir(parents=True, exist_ok=True)

    def fetch_window(bounds: tuple[int, int]):
        window_start, window_end = bounds
        chunk_path = checkpoint_root / f"{window_start}_{window_end}.jsonl"
        audit_path = checkpoint_root / f"{window_start}_{window_end}.audit.json"
        if chunk_path.exists() and audit_path.exists():
            rows = list(read_jsonl(chunk_path))
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            pages = [
                PageAudit(
                    str(page["endpoint"]),
                    page.get("start"),
                    page.get("end_inclusive"),
                    int(page["offset"]),
                    int(page["count"]),
                )
                for page in audit.get("canonical_pages", [])
            ]
            return rows, pages, int(audit.get("discarded_probe_records", 0))
        rows, pages, discarded = _fetch_activity_window(
            client,
            wallet,
            window_start,
            window_end,
            activity_types=activity_types,
        )
        rows.sort(
            key=lambda row: (
                int(row.get("timestamp", 0)),
                str(row.get("transactionHash") or ""),
                str(row.get("type") or ""),
                str(row.get("asset") or ""),
                str(row.get("side") or ""),
                str(row.get("outcome") or ""),
                str(row.get("price") or ""),
                str(row.get("size") or ""),
            )
        )
        write_jsonl(chunk_path, rows)
        write_json(
            audit_path,
            {
                "start": window_start,
                "end_exclusive": window_end,
                "canonical_records": len(rows),
                "canonical_pages": [page.as_dict() for page in pages],
                "discarded_probe_records": discarded,
                "sha256": sha256_file(chunk_path),
            },
        )
        return rows, pages, discarded

    with partial.open("w", encoding="utf-8", newline="\n") as handle:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(window_workers, len(windows)))
        ) as executor:
            captured_windows = executor.map(fetch_window, windows)
            for rows, pages, discarded in captured_windows:
                rows.sort(
                key=lambda row: (
                    int(row.get("timestamp", 0)),
                    str(row.get("transactionHash") or ""),
                    str(row.get("type") or ""),
                    str(row.get("asset") or ""),
                    str(row.get("side") or ""),
                    str(row.get("outcome") or ""),
                    str(row.get("price") or ""),
                    str(row.get("size") or ""),
                )
            )
                for row in rows:
                    digest = row_digest(row)
                    if digest in seen_exact:
                        exact_duplicates += 1
                    else:
                        seen_exact.add(digest)
                    timestamp = int(row["timestamp"])
                    min_timestamp = timestamp if min_timestamp is None else min(min_timestamp, timestamp)
                    max_timestamp = timestamp if max_timestamp is None else max(max_timestamp, timestamp)
                    handle.write(stable_json(row) + "\n")
                    canonical_rows += 1
                page_audits.extend(pages)
                discarded_probe_records += discarded
                handle.flush()
        os.fsync(handle.fileno())
    partial.replace(output_path)
    return {
        "capture_status": "CREATED",
        "canonical_api_records": canonical_rows,
        "processed_records": canonical_rows,
        "exact_duplicate_records": exact_duplicates,
        "unique_exact_records": len(seen_exact),
        "min_timestamp": min_timestamp,
        "max_timestamp": max_timestamp,
        "discarded_probe_records_due_to_adaptive_split": discarded_probe_records,
        "canonical_pages": [page.as_dict() for page in page_audits],
        "canonical_page_count": len(page_audits),
        "sha256": sha256_file(output_path),
    }


def audit_activity_rows(
    rows: Sequence[dict[str, Any]],
    wallet: str,
    start: int,
    cutoff_inclusive: int,
    *,
    pages: Sequence[PageAudit],
    discarded_probe_records: int,
    capture_status: str,
) -> dict[str, Any]:
    exact = set()
    duplicates = 0
    timestamps = []
    for row in rows:
        if str(row.get("proxyWallet") or "").lower() != wallet.lower():
            raise SportsResearchError("Archivo de activity contiene otra wallet")
        timestamp = int(row.get("timestamp", -1))
        if not start <= timestamp <= cutoff_inclusive:
            raise SportsResearchError("Archivo de activity está fuera del corte")
        digest = row_digest(row)
        if digest in exact:
            duplicates += 1
        exact.add(digest)
        timestamps.append(timestamp)
    return {
        "capture_status": capture_status,
        "canonical_api_records": len(rows),
        "processed_records": len(rows),
        "exact_duplicate_records": duplicates,
        "unique_exact_records": len(exact),
        "min_timestamp": min(timestamps) if timestamps else None,
        "max_timestamp": max(timestamps) if timestamps else None,
        "discarded_probe_records_due_to_adaptive_split": discarded_probe_records,
        "canonical_pages": [page.as_dict() for page in pages],
        "canonical_page_count": len(pages),
        "sha256": None,
    }


def paginate_offset_endpoint(
    client: PublicApiClient,
    path: str,
    params: dict[str, Any],
    *,
    limit: int,
    max_offset: int,
) -> tuple[list[dict[str, Any]], list[PageAudit], bool]:
    rows: list[dict[str, Any]] = []
    pages: list[PageAudit] = []
    offset = 0
    while True:
        page = client.get_json(
            _url(DATA_API, path, {**params, "limit": limit, "offset": offset})
        )
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise SportsResearchError(f"Página inválida: {path}")
        pages.append(PageAudit(path.lstrip("/"), None, None, offset, len(page)))
        rows.extend(page)
        if len(page) < limit:
            return rows, pages, True
        if offset == max_offset:
            return rows, pages, False
        offset = min(max_offset, offset + limit)


def paginate_offset_endpoint_parallel(
    client: PublicApiClient,
    path: str,
    params: dict[str, Any],
    *,
    limit: int,
    max_offset: int,
    max_workers: int = 12,
) -> tuple[list[dict[str, Any]], list[PageAudit], bool]:
    """Finds the terminal page, then downloads every page with bounded concurrency."""
    max_index = max_offset // limit
    cache: dict[int, list[dict[str, Any]]] = {}
    cache_lock = threading.Lock()

    def get_page(index: int) -> list[dict[str, Any]]:
        with cache_lock:
            cached = cache.get(index)
        if cached is not None:
            return cached
        offset = index * limit
        page = client.get_json(
            _url(DATA_API, path, {**params, "limit": limit, "offset": offset})
        )
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise SportsResearchError(f"Página inválida: {path}")
        with cache_lock:
            cache[index] = page
        return page

    first = get_page(0)
    if not first:
        return [], [PageAudit(path.lstrip("/"), None, None, 0, 0)], True
    last_allowed = get_page(max_index)
    if last_allowed:
        last_index = max_index
        complete = len(last_allowed) < limit
    else:
        low = 0
        high = max_index
        while high - low > 1:
            midpoint = (low + high) // 2
            if get_page(midpoint):
                low = midpoint
            else:
                high = midpoint
        last_index = low
        complete = True

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        pages_by_index = list(executor.map(get_page, range(last_index + 1)))
    rows: list[dict[str, Any]] = []
    audits: list[PageAudit] = []
    for index, page in enumerate(pages_by_index):
        rows.extend(page)
        audits.append(PageAudit(path.lstrip("/"), None, None, index * limit, len(page)))
    return rows, audits, complete


def fetch_positions(
    client: PublicApiClient, wallet: str
) -> tuple[list[dict[str, Any]], list[PageAudit], bool]:
    return paginate_offset_endpoint(
        client,
        "/positions",
        {
            "user": wallet,
            "sizeThreshold": 0,
            "includeArchived": "true",
            "sortBy": "TOKENS",
            "sortDirection": "DESC",
        },
        limit=500,
        max_offset=10_000,
    )


def fetch_closed_positions(
    client: PublicApiClient, wallet: str
) -> tuple[list[dict[str, Any]], list[PageAudit], bool]:
    return paginate_offset_endpoint_parallel(
        client,
        "/closed-positions",
        {"user": wallet, "sortBy": "TIMESTAMP", "sortDirection": "ASC"},
        limit=50,
        max_offset=100_000,
    )


def fetch_markets(
    client: PublicApiClient,
    condition_ids: Sequence[str],
    *,
    batch_size: int = 40,
    checkpoint_root: Path | None = None,
    max_workers: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    requests: list[dict[str, Any]] = []
    tasks: list[tuple[int, list[str], bool]] = []
    for index in range(0, len(condition_ids), batch_size):
        batch_number = index // batch_size
        batch = list(condition_ids[index : index + batch_size])
        for closed in (False, True):
            tasks.append((batch_number, batch, closed))
    if checkpoint_root is not None:
        checkpoint_root.mkdir(parents=True, exist_ok=True)

    def fetch_task(task: tuple[int, list[str], bool]):
        batch_number, batch, closed = task
        checkpoint = (
            checkpoint_root / f"batch_{batch_number:05d}_{'closed' if closed else 'open'}.json"
            if checkpoint_root is not None
            else None
        )
        if checkpoint is not None and checkpoint.exists():
            page = json.loads(checkpoint.read_text(encoding="utf-8"))
        else:
            query = urllib.parse.urlencode(
                [
                    ("closed", str(closed).lower()),
                    ("include_tag", "true"),
                    ("limit", len(batch)),
                    *(("condition_ids", condition) for condition in batch),
                ],
                doseq=True,
            )
            url = f"{GAMMA_API}/markets?{query}"
            page = client.get_json(url)
            if checkpoint is not None:
                write_json(checkpoint, page)
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise SportsResearchError("Gamma /markets devolvió una página inválida")
        return batch_number, batch, closed, page

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(max_workers, len(tasks)))
    ) as executor:
        results = executor.map(fetch_task, tasks)
        for batch_number, batch, closed, page in results:
            requests.append(
                {
                    "batch": batch_number,
                    "closed": closed,
                    "conditions": len(batch),
                    "returned": len(page),
                }
            )
            for market in page:
                condition = str(market.get("conditionId") or "").lower()
                if condition:
                    found[condition] = market
    missing = [condition for condition in condition_ids if condition.lower() not in found]
    request_summary = {
        "query_count": len(requests),
        "query_returned_records": sum(row["returned"] for row in requests),
        "zero_result_queries": sum(1 for row in requests if row["returned"] == 0),
        "open_queries": sum(1 for row in requests if not row["closed"]),
        "closed_queries": sum(1 for row in requests if row["closed"]),
    }
    return (
        [found[key] for key in sorted(found)],
        {
            "requested_unique_conditions": len(condition_ids),
            "returned_unique_conditions": len(found),
            "missing_conditions": missing,
            "complete": not missing,
            "request_summary": request_summary,
        },
    )


def _tag_tokens(market: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for tag in market.get("tags") or []:
        if isinstance(tag, dict):
            for field in ("id", "slug", "label"):
                value = tag.get(field)
                if value is not None:
                    tokens.add(str(value).casefold())
    for event in market.get("events") or []:
        if not isinstance(event, dict):
            continue
        for tag in event.get("tags") or []:
            if isinstance(tag, dict):
                for field in ("id", "slug", "label"):
                    value = tag.get(field)
                    if value is not None:
                        tokens.add(str(value).casefold())
    return tokens


def _event_series_ids(market: dict[str, Any]) -> set[str]:
    series_ids: set[str] = set()
    for event in market.get("events") or []:
        if not isinstance(event, dict):
            continue
        for series in event.get("series") or []:
            if isinstance(series, dict) and series.get("id") is not None:
                series_ids.add(str(series["id"]))
    return series_ids


def classify_market(
    market: dict[str, Any], sports_metadata: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    tags = _tag_tokens(market)
    series_ids = _event_series_ids(market)
    market_type = str(market.get("sportsMarketType") or "").strip()
    matched: list[tuple[int, dict[str, Any]]] = []
    for sport in sports_metadata:
        primary = str(sport.get("primaryTagId") or "").casefold()
        series = str(sport.get("series") or "")
        score = 0
        if series and series in series_ids:
            score += 4
        if primary and primary in tags:
            score += 3
        if score:
            matched.append((score, sport))
    matched.sort(
        key=lambda item: (
            -item[0],
            str(item[1].get("name") or item[1].get("sport") or ""),
        )
    )
    best = matched[0][1] if matched else None
    evidence_text = " ".join(
        [
            str(market.get("question") or ""),
            str(market.get("slug") or ""),
            market_type,
            " ".join(sorted(tags)),
            str((best or {}).get("sport") or ""),
            str((best or {}).get("name") or ""),
        ]
    ).casefold()
    is_esports = any(marker in evidence_text for marker in ESPORTS_MARKERS)
    is_sports = bool(best or market_type)
    if is_esports:
        scope = "ESPORTS_EXCLUDED"
    elif is_sports:
        scope = "SPORTS_INCLUDED"
    else:
        scope = "NON_SPORTS_EXCLUDED"
    sport_code = str((best or {}).get("sport") or "") or None
    league = str((best or {}).get("name") or "") or None
    normalized_type = market_type.casefold()
    if "spread" in normalized_type or "handicap" in normalized_type:
        bet_family = "spread_handicap"
    elif "total" in normalized_type or "over_under" in normalized_type:
        bet_family = "total"
    elif "moneyline" in normalized_type or normalized_type.endswith("winner"):
        bet_family = "moneyline_winner"
    elif any(token in normalized_type for token in ("player", "assists", "points", "rebounds", "touchdown", "strikeout")):
        bet_family = "player_prop"
    elif any(token in normalized_type for token in ("set", "game", "quarter", "half", "inning", "period")):
        bet_family = "segment_prop"
    elif normalized_type:
        bet_family = "other_sports_type"
    else:
        bet_family = "unclassified"
    return {
        "scope": scope,
        "is_sports": is_sports,
        "is_esports": is_esports,
        "sport_code": sport_code,
        "league": league,
        "sports_market_type": market_type or None,
        "bet_family": bet_family,
        "matched_sports_metadata": [
            {
                "score": score,
                "sport": row.get("sport"),
                "name": row.get("name"),
                "series": row.get("series"),
                "primaryTagId": row.get("primaryTagId"),
            }
            for score, row in matched
        ],
        "evidence_tags": sorted(tags),
    }


def market_start_unix(market: dict[str, Any]) -> int | None:
    candidates: list[Any] = [market.get("gameStartTime"), market.get("eventStartTime")]
    for event in market.get("events") or []:
        if isinstance(event, dict):
            candidates.extend(
                [event.get("startTime"), event.get("eventDate"), event.get("startDate")]
            )
    for value in candidates:
        if not value:
            continue
        try:
            text = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            continue
    return None


def derive_sports_trades(
    identities: Sequence[dict[str, Any]],
    activity_paths: dict[str, Path],
    markets: Sequence[dict[str, Any]],
    sports_metadata: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    identity_map = {row["key"]: row for row in identities}
    market_map = {
        str(row.get("conditionId") or "").lower(): row
        for row in markets
        if row.get("conditionId")
    }
    catalog: list[dict[str, Any]] = []
    classifications: dict[str, dict[str, Any]] = {}
    for condition, market in sorted(market_map.items()):
        classification = classify_market(market, sports_metadata)
        classifications[condition] = classification
        start_ts = market_start_unix(market)
        catalog.append(
            {
                "condition_id": condition,
                "market_id": market.get("id"),
                "slug": market.get("slug"),
                "question": market.get("question"),
                "event_slug": ((market.get("events") or [{}])[0] or {}).get("slug"),
                "event_title": ((market.get("events") or [{}])[0] or {}).get("title"),
                "market_start_unix": start_ts,
                "market_start_utc": utc_iso(start_ts) if start_ts is not None else None,
                "end_date": market.get("endDate"),
                "closed": market.get("closed"),
                "outcomes": market.get("outcomes"),
                "outcome_prices": market.get("outcomePrices"),
                "clob_token_ids": market.get("clobTokenIds"),
                "seconds_delay": market.get("secondsDelay"),
                **{key: value for key, value in classification.items() if key != "matched_sports_metadata" and key != "evidence_tags"},
            }
        )
    sports_rows: list[dict[str, Any]] = []
    counts = {"all_activity": 0, "trade_activity": 0, "sports_trades": 0, "esports_trades": 0, "non_sports_trades": 0, "metadata_missing_trades": 0}
    by_trader: dict[str, dict[str, int]] = {}
    for key, path in activity_paths.items():
        identity = identity_map[key]
        trader_counts = {name: 0 for name in counts}
        for row in read_jsonl(path):
            counts["all_activity"] += 1
            trader_counts["all_activity"] += 1
            if row.get("type") != "TRADE":
                continue
            counts["trade_activity"] += 1
            trader_counts["trade_activity"] += 1
            condition = str(row.get("conditionId") or "").lower()
            market = market_map.get(condition)
            classification = classifications.get(condition)
            if market is None or classification is None:
                counts["metadata_missing_trades"] += 1
                trader_counts["metadata_missing_trades"] += 1
                continue
            if classification["scope"] == "ESPORTS_EXCLUDED":
                counts["esports_trades"] += 1
                trader_counts["esports_trades"] += 1
                continue
            if classification["scope"] != "SPORTS_INCLUDED":
                counts["non_sports_trades"] += 1
                trader_counts["non_sports_trades"] += 1
                continue
            counts["sports_trades"] += 1
            trader_counts["sports_trades"] += 1
            timestamp = int(row["timestamp"])
            start_ts = market_start_unix(market)
            if start_ts is None:
                timing = "UNKNOWN"
                seconds_to_start = None
            else:
                seconds_to_start = start_ts - timestamp
                timing = "PREGAME" if seconds_to_start > 0 else "LIVE_OR_AFTER_START"
            size = _number(row.get("size"), "size")
            price = _number(row.get("price"), "price")
            sports_rows.append(
                {
                    "trader_key": key,
                    "trader_name": identity["profile_name"],
                    "proxy_wallet": identity["proxy_wallet"],
                    "timestamp": timestamp,
                    "timestamp_utc": utc_iso(timestamp),
                    "transaction_hash": row.get("transactionHash"),
                    "condition_id": condition,
                    "market_id": market.get("id"),
                    "event_id": ((market.get("events") or [{}])[0] or {}).get("id"),
                    "slug": row.get("slug") or market.get("slug"),
                    "event_slug": row.get("eventSlug") or ((market.get("events") or [{}])[0] or {}).get("slug"),
                    "title": row.get("title") or market.get("question"),
                    "sport_code": classification["sport_code"],
                    "league": classification["league"],
                    "sports_market_type": classification["sports_market_type"],
                    "bet_family": classification["bet_family"],
                    "timing": timing,
                    "seconds_to_start": seconds_to_start,
                    "side": row.get("side"),
                    "outcome": row.get("outcome"),
                    "outcome_index": row.get("outcomeIndex"),
                    "asset": row.get("asset"),
                    "size": size,
                    "price": price,
                    "notional_usd": _number(row.get("usdcSize", size * price), "usdcSize"),
                    "is_combo": bool(row.get("isCombo", False)),
                }
            )
        by_trader[key] = trader_counts
    sports_rows.sort(
        key=lambda row: (
            row["timestamp"],
            row["trader_key"],
            str(row["transaction_hash"] or ""),
            str(row["asset"] or ""),
        )
    )
    return sports_rows, catalog, {"totals": counts, "by_trader": by_trader}


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with partial.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def protocol_payload() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": "0.1.0",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "cutoff": {
            "inclusive_unix": CUTOFF_INCLUSIVE_UNIX,
            "inclusive_utc": CUTOFF_UTC,
            "source": f"{CLOB_API}/time",
        },
        "scope": {
            "included": "Mercados deportivos verificados por metadatos Gamma; todos los fills públicos recuperables de las cuatro wallets hasta el corte.",
            "excluded": "Política, cripto, macro, cultura, elecciones, clima y cualquier otro sector no deportivo.",
            "esports": "Se identifica y cuantifica por separado, pero se excluye del universo deportivo principal.",
            "sampling": False,
        },
        "sources": {
            "profiles": f"{GAMMA_API}/public-profile",
            "activity": f"{DATA_API}/activity",
            "positions": f"{DATA_API}/positions",
            "closed_positions": f"{DATA_API}/closed-positions",
            "markets": f"{GAMMA_API}/markets",
            "sports_metadata": f"{GAMMA_API}/sports",
            "sports_market_types": f"{GAMMA_API}/sports/market-types",
            "price_history": f"{CLOB_API}/prices-history",
        },
        "pagination": {
            "activity": "Ventanas iniciales de 24h, offset 0..5000, división binaria si la última página está llena.",
            "positions": "limit=500, offset 0..10000.",
            "closed_positions": "limit=50, offset 0..100000.",
            "markets": "Batches de conditionId consultados con closed=false y closed=true.",
        },
        "known_identification_limits": [
            "Activity no expone órdenes no llenadas, cancelaciones ni prioridad de cola.",
            "Prices-history conserva precios agregados, no libros históricos completos.",
            "Maker/taker, fees y rebates solo se atribuirán cuando exista evidencia pública suficiente.",
        ],
        "safety": {
            "paper_only": True,
            "wallet_connection_required": False,
            "credentials_required": False,
            "orders_enabled": False,
            "real_money": "BLOQUEADO",
        },
        "traders": [dict(row) for row in TRADERS],
    }


def prepare(output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    protocol = protocol_payload()
    target = output_root / "protocol.json"
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        comparable_existing = dict(existing)
        comparable_protocol = dict(protocol)
        comparable_existing.pop("created_at_utc", None)
        comparable_protocol.pop("created_at_utc", None)
        if comparable_existing != comparable_protocol:
            raise SportsResearchError("El protocolo existente no coincide")
        status = "VERIFIED_EXISTING"
    else:
        write_json(target, protocol)
        status = "CREATED"
    return {"status": status, "path": str(target), "sha256": sha256_file(target)}


def capture(output_root: Path, client: PublicApiClient | None = None) -> dict[str, Any]:
    client = client or PublicApiClient()
    protocol_path = output_root / "protocol.json"
    if not protocol_path.exists():
        raise SportsResearchError("Falta protocol.json; ejecute prepare primero")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if protocol.get("schema") != SCHEMA:
        raise SportsResearchError("Protocolo incompatible")
    identities = [verify_identity(client, spec) for spec in TRADERS]
    raw = output_root / "raw"
    derived = output_root / "derived"
    audit_dir = output_root / "audit"
    write_json(raw / "identities.json", identities)
    sports_metadata = client.get_json(f"{GAMMA_API}/sports")
    market_types = client.get_json(f"{GAMMA_API}/sports/market-types")
    if not isinstance(sports_metadata, list) or not isinstance(market_types, dict):
        raise SportsResearchError("Catálogo deportivo oficial inválido")
    write_json(raw / "sports_metadata.json", sports_metadata)
    write_json(raw / "sports_market_types.json", market_types)

    capture_audit: dict[str, Any] = {}
    activity_paths: dict[str, Path] = {}
    all_conditions: set[str] = set()
    for identity in identities:
        key = identity["key"]
        wallet = identity["proxy_wallet"]
        first, last = find_activity_bounds(client, wallet, CUTOFF_INCLUSIVE_UNIX)
        if first is None or last is None:
            capture_audit[key] = {"activity": {"canonical_api_records": 0}}
            continue
        activity_path = raw / f"{key}_activity.jsonl"
        activity_paths[key] = activity_path
        activity_audit = capture_activity(
            client, wallet, first, CUTOFF_INCLUSIVE_UNIX, activity_path
        )
        positions_path = raw / f"{key}_positions.jsonl"
        closed_path = raw / f"{key}_closed_positions.jsonl"
        if positions_path.exists():
            positions = list(read_jsonl(positions_path))
            positions_pages = []
            positions_complete = len(positions) < 10_500
            positions_status = "VERIFIED_EXISTING_FROM_COMPLETED_CAPTURE"
        else:
            positions, positions_pages, positions_complete = fetch_positions(client, wallet)
            write_jsonl(positions_path, positions)
            positions_status = "CREATED"
        if closed_path.exists():
            closed = list(read_jsonl(closed_path))
            closed_pages = []
            closed_complete = len(closed) < 100_050
            closed_status = "VERIFIED_EXISTING_FROM_COMPLETED_CAPTURE"
        else:
            closed, closed_pages, closed_complete = fetch_closed_positions(client, wallet)
            write_jsonl(closed_path, closed)
            closed_status = "CREATED"
        for row in read_jsonl(activity_path):
            condition = str(row.get("conditionId") or "").lower()
            if condition:
                all_conditions.add(condition)
        capture_audit[key] = {
            "first_trade_timestamp": first,
            "first_trade_utc": utc_iso(first),
            "last_trade_timestamp": last,
            "last_trade_utc": utc_iso(last),
            "activity": activity_audit,
            "positions": {
                "capture_status": positions_status,
                "api_records": len(positions),
                "processed_records": len(positions),
                "complete_within_documented_offset": positions_complete,
                "pages": [page.as_dict() for page in positions_pages],
            },
            "closed_positions": {
                "capture_status": closed_status,
                "api_records": len(closed),
                "processed_records": len(closed),
                "complete_within_documented_offset": closed_complete,
                "pages": [page.as_dict() for page in closed_pages],
            },
        }

    markets, market_audit = fetch_markets(
        client,
        sorted(all_conditions),
        checkpoint_root=raw / "checkpoints" / "markets_repeated_params_v2",
    )
    write_jsonl(raw / "markets.jsonl", markets)
    sports_rows, catalog, classification_audit = derive_sports_trades(
        identities, activity_paths, markets, sports_metadata
    )
    write_jsonl(derived / "master_sports_trades.jsonl", sports_rows)
    write_csv(derived / "master_sports_trades.csv", sports_rows)
    write_jsonl(derived / "market_catalog.jsonl", catalog)
    write_csv(derived / "market_catalog.csv", catalog)
    manifest = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cutoff_inclusive_unix": CUTOFF_INCLUSIVE_UNIX,
        "cutoff_utc": CUTOFF_UTC,
        "identities": identities,
        "capture": capture_audit,
        "market_metadata": market_audit,
        "classification": classification_audit,
        "api_request_count_this_run": client.requests,
        "files": {},
        "safety": protocol["safety"],
    }
    for path in sorted(output_root.rglob("*")):
        if path.is_file() and ".partial" not in path.name:
            if "checkpoints" in path.relative_to(output_root).parts:
                continue
            manifest["files"][str(path.relative_to(output_root))] = {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
    write_json(audit_dir / "coverage_manifest.json", manifest)
    return manifest


__all__ = [
    "ACTIVITY_TYPES",
    "CUTOFF_INCLUSIVE_UNIX",
    "CUTOFF_UTC",
    "PublicApiClient",
    "SCHEMA",
    "SportsResearchError",
    "TRADERS",
    "capture",
    "capture_activity",
    "classify_market",
    "derive_sports_trades",
    "fetch_markets",
    "prepare",
    "protocol_payload",
]

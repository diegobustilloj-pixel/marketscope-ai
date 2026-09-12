from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any

from polymarket_bot.config import Settings
from polymarket_bot.domain import MarketDefinition, RawEvent, Sequence
from polymarket_bot.storage import AsyncEventWriter, SQLiteStore


class DiscoveryError(RuntimeError):
    pass


def _json_list(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DiscoveryError(f"{field} no contiene JSON válido") from exc
    if not isinstance(value, list):
        raise DiscoveryError(f"{field} no es una lista")
    return tuple(str(item) for item in value)


def parse_market_payload(
    payload: dict[str, Any], payload_raw: str
) -> MarketDefinition:
    if "markets" in payload:
        markets = payload.get("markets")
        if not isinstance(markets, list) or not markets:
            raise DiscoveryError("El evento no contiene mercados")
        event_slug = payload.get("slug")
        matching = [
            item
            for item in markets
            if isinstance(item, dict) and item.get("slug") == event_slug
        ]
        market = matching[0] if matching else markets[0]
        event_id = payload.get("id")
        event_resolution = payload.get("resolutionSource")
        event_start = payload.get("startDate")
        event_end = payload.get("endDate")
    else:
        market = payload
        event_id = None
        event_resolution = None
        event_start = None
        event_end = None

    if not isinstance(market, dict):
        raise DiscoveryError("Metadatos de mercado inválidos")
    condition_id = market.get("conditionId")
    slug = market.get("slug") or payload.get("slug")
    if not condition_id or not slug:
        raise DiscoveryError("Faltan conditionId o slug")

    outcomes = _json_list(market.get("outcomes"), "outcomes")
    token_ids = _json_list(market.get("clobTokenIds"), "clobTokenIds")
    if len(outcomes) != 2 or len(token_ids) != 2:
        raise DiscoveryError(
            "El mercado BTC Up/Down debe tener dos outcomes y dos token IDs"
        )

    return MarketDefinition(
        slug=str(slug),
        event_id=str(event_id) if event_id is not None else None,
        condition_id=str(condition_id),
        question=str(market.get("question") or payload.get("title") or ""),
        start_at=market.get("startDate") or event_start,
        end_at=market.get("endDate") or event_end,
        resolution_source=market.get("resolutionSource")
        or event_resolution,
        outcomes=outcomes,
        token_ids=token_ids,
        active=bool(market.get("active", payload.get("active", False))),
        closed=bool(market.get("closed", payload.get("closed", False))),
        accepting_orders=bool(market.get("acceptingOrders", False)),
        payload_raw=payload_raw,
    )


def candidate_btc_5m_slugs(now_epoch: int | None = None) -> tuple[str, ...]:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    block = now - (now % 300)
    starts = (block, block - 300, block + 300, block - 600)
    return tuple(f"btc-updown-5m-{start}" for start in starts)


class GammaDiscovery:
    def __init__(
        self,
        settings: Settings,
        store: SQLiteStore,
        writer: AsyncEventWriter,
        sequence: Sequence,
    ) -> None:
        self.settings = settings
        self.store = store
        self.writer = writer
        self.sequence = sequence

    def _fetch_sync(self, path: str) -> tuple[dict[str, Any], str]:
        url = f"{self.settings.gamma_base_url}{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-quant-bot-phase1/0.1",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.http_timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise DiscoveryError("Mercado no encontrado") from exc
            raise DiscoveryError(
                f"Gamma respondió HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DiscoveryError(f"No se pudo conectar con Gamma: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DiscoveryError("Gamma devolvió JSON inválido") from exc
        if not isinstance(payload, dict):
            raise DiscoveryError("Gamma devolvió una estructura inesperada")
        return payload, raw

    async def fetch_slug(self, slug: str) -> MarketDefinition:
        escaped = urllib.parse.quote(slug, safe="")
        errors: list[str] = []
        for resource in ("events", "markets"):
            try:
                payload, raw = await asyncio.to_thread(
                    self._fetch_sync, f"/{resource}/slug/{escaped}"
                )
            except DiscoveryError as exc:
                errors.append(f"{resource}: {exc}")
                continue
            event = RawEvent.create(
                source="gamma",
                default_stream=f"{resource}_metadata",
                payload_raw=raw,
                sequence=self.sequence.next(),
            )
            await self.writer.submit(event)
            market = parse_market_payload(payload, raw)
            await asyncio.to_thread(self.store.save_market, market)
            return market
        raise DiscoveryError("; ".join(errors))

    async def discover(
        self, preferred_slug: str | None = None
    ) -> MarketDefinition:
        candidates: Iterable[str] = (
            (preferred_slug,)
            if preferred_slug is not None
            else candidate_btc_5m_slugs()
        )
        failures: list[str] = []
        fallback: MarketDefinition | None = None
        for slug in candidates:
            try:
                market = await self.fetch_slug(slug)
            except DiscoveryError as exc:
                failures.append(f"{slug}: {exc}")
                continue
            if (
                market.active
                and not market.closed
                and market.accepting_orders
            ):
                return market
            if fallback is None:
                fallback = market
        if fallback is not None:
            return fallback
        raise DiscoveryError(
            "No se encontró un mercado BTC 5m. " + " | ".join(failures)
        )


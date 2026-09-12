from __future__ import annotations

from polymarket_bot.collectors.websocket import (
    ResilientWebSocketCollector,
    WebSocketSpec,
)
from polymarket_bot.config import Settings
from polymarket_bot.domain import MarketDefinition, Sequence
from polymarket_bot.storage import AsyncEventWriter, SQLiteStore


def build_clob_collector(
    settings: Settings,
    store: SQLiteStore,
    writer: AsyncEventWriter,
    sequence: Sequence,
    market: MarketDefinition,
) -> ResilientWebSocketCollector:
    subscription = {
        "assets_ids": list(market.token_ids),
        "type": "market",
        "custom_feature_enabled": True,
    }
    spec = WebSocketSpec(
        component=f"clob-{market.slug}",
        source="clob",
        endpoint=settings.clob_ws_url,
        subscription=subscription,
        heartbeat_text="PING",
        heartbeat_seconds=10.0,
        default_stream="market",
        use_proxy=settings.ws_use_proxy,
        market_id=market.condition_id,
    )
    return ResilientWebSocketCollector(
        spec,
        store,
        writer,
        sequence,
        reconnect_max_seconds=settings.reconnect_max_seconds,
    )

from __future__ import annotations

from polymarket_bot.collectors.websocket import (
    ResilientWebSocketCollector,
    WebSocketSpec,
)
from polymarket_bot.config import Settings
from polymarket_bot.domain import Sequence
from polymarket_bot.storage import AsyncEventWriter, SQLiteStore


def build_binance_collector(
    settings: Settings,
    store: SQLiteStore,
    writer: AsyncEventWriter,
    sequence: Sequence,
) -> ResilientWebSocketCollector:
    """BTC/USDT aggTrades desde el endpoint oficial solo-market-data."""
    spec = WebSocketSpec(
        component="binance-btcusdt-aggtrade",
        source="binance",
        endpoint=settings.binance_ws_url,
        subscription=None,
        heartbeat_text=None,
        heartbeat_seconds=None,
        default_stream="aggTrade",
        use_proxy=settings.ws_use_proxy,
    )
    return ResilientWebSocketCollector(
        spec,
        store,
        writer,
        sequence,
        reconnect_max_seconds=settings.reconnect_max_seconds,
    )

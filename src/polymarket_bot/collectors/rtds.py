from __future__ import annotations

import json

from polymarket_bot.collectors.websocket import (
    ResilientWebSocketCollector,
    WebSocketSpec,
)
from polymarket_bot.config import Settings
from polymarket_bot.domain import Sequence
from polymarket_bot.resolution_contract import TWAP_TOPIC_BY_WINDOW
from polymarket_bot.storage import AsyncEventWriter, SQLiteStore


def build_rtds_collector(
    settings: Settings,
    store: SQLiteStore,
    writer: AsyncEventWriter,
    sequence: Sequence,
) -> ResilientWebSocketCollector:
    subscription = {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": json.dumps(
                    {"symbol": "btc/usd"}, separators=(",", ":")
                ),
            },
            *(
                {
                    "topic": topic,
                    "type": "update",
                    "filters": json.dumps(
                        {"symbol": "btc/usd"}, separators=(",", ":")
                    ),
                }
                for topic in TWAP_TOPIC_BY_WINDOW.values()
            ),
        ],
    }
    spec = WebSocketSpec(
        component="rtds-btc",
        source="rtds",
        endpoint=settings.rtds_ws_url,
        subscription=subscription,
        heartbeat_text="PING",
        heartbeat_seconds=5.0,
        default_stream="crypto_price",
        use_proxy=settings.ws_use_proxy,
    )
    return ResilientWebSocketCollector(
        spec,
        store,
        writer,
        sequence,
        reconnect_max_seconds=settings.reconnect_max_seconds,
    )

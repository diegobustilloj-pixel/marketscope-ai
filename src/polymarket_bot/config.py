from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} debe ser mayor que cero")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} debe ser true o false")


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    gamma_base_url: str
    clob_ws_url: str
    rtds_ws_url: str
    binance_ws_url: str
    http_timeout_seconds: float
    reconnect_max_seconds: float
    ws_use_proxy: bool
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = Path(
            os.getenv("PM_DB_PATH", "data/polymarket_phase1_v4.db")
        ).expanduser()
        return cls(
            db_path=db_path,
            gamma_base_url=os.getenv(
                "PM_GAMMA_BASE_URL", "https://gamma-api.polymarket.com"
            ).rstrip("/"),
            clob_ws_url=os.getenv(
                "PM_CLOB_WS_URL",
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            ),
            rtds_ws_url=os.getenv(
                "PM_RTDS_WS_URL", "wss://ws-live-data.polymarket.com"
            ),
            binance_ws_url=os.getenv(
                "PM_BINANCE_WS_URL",
                (
                    "wss://data-stream.binance.vision:443/"
                    "ws/btcusdt@aggTrade"
                ),
            ),
            http_timeout_seconds=_positive_float(
                "PM_HTTP_TIMEOUT_SECONDS", 10.0
            ),
            reconnect_max_seconds=_positive_float(
                "PM_RECONNECT_MAX_SECONDS", 30.0
            ),
            ws_use_proxy=_boolean("PM_WS_USE_PROXY", False),
            log_level=os.getenv("PM_LOG_LEVEL", "INFO").upper(),
        )

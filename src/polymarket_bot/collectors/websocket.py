from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from polymarket_bot.domain import RawEvent, Sequence
from polymarket_bot.storage import AsyncEventWriter, SQLiteStore


@dataclass(frozen=True, slots=True)
class WebSocketSpec:
    component: str
    source: str
    endpoint: str
    subscription: dict[str, Any] | None
    heartbeat_text: str | None
    heartbeat_seconds: float | None
    default_stream: str
    use_proxy: bool = False
    market_id: str | None = None


class ResilientWebSocketCollector:
    def __init__(
        self,
        spec: WebSocketSpec,
        store: SQLiteStore,
        writer: AsyncEventWriter,
        sequence: Sequence,
        *,
        reconnect_max_seconds: float,
    ) -> None:
        self.spec = spec
        self.store = store
        self.writer = writer
        self.sequence = sequence
        self.reconnect_max_seconds = reconnect_max_seconds
        self.logger = logging.getLogger(spec.component)

    async def _record_system(
        self, stream: str, payload: dict[str, Any]
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        await self.writer.submit(
            RawEvent.create(
                source="system",
                default_stream=stream,
                payload_raw=raw,
                sequence=self.sequence.next(),
                market_id=self.spec.market_id,
            )
        )

    async def _heartbeat(self, websocket: Any) -> None:
        if (
            self.spec.heartbeat_seconds is None
            or self.spec.heartbeat_text is None
        ):
            return
        while True:
            await asyncio.sleep(self.spec.heartbeat_seconds)
            await websocket.send(self.spec.heartbeat_text)

    async def run(self, stop_event: asyncio.Event) -> None:
        run_id = await asyncio.to_thread(
            self.store.start_run, self.spec.component
        )
        attempt = 0
        final_status = "STOPPED"
        final_error: str | None = None
        try:
            while not stop_event.is_set():
                try:
                    async with connect(
                        self.spec.endpoint,
                        ping_interval=None,
                        open_timeout=12,
                        close_timeout=5,
                        max_size=8 * 1024 * 1024,
                        max_queue=4096,
                        user_agent_header="polymarket-quant-bot/0.6.1",
                        proxy=True if self.spec.use_proxy else None,
                    ) as websocket:
                        if self.spec.subscription is not None:
                            await websocket.send(
                                json.dumps(
                                    self.spec.subscription,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            )
                        await self._record_system(
                            "collector_connected",
                            {
                                "component": self.spec.component,
                                "attempt": attempt,
                            },
                        )
                        attempt = 0
                        heartbeat_task = (
                            asyncio.create_task(
                                self._heartbeat(websocket),
                                name=f"{self.spec.component}-heartbeat",
                            )
                            if self.spec.heartbeat_text is not None
                            and self.spec.heartbeat_seconds is not None
                            else None
                        )
                        try:
                            async for message in websocket:
                                if stop_event.is_set():
                                    break
                                raw = (
                                    message.decode("utf-8", errors="replace")
                                    if isinstance(message, bytes)
                                    else str(message)
                                )
                                await self.writer.submit(
                                    RawEvent.create(
                                        source=self.spec.source,
                                        default_stream=self.spec.default_stream,
                                        payload_raw=raw,
                                        sequence=self.sequence.next(),
                                        market_id=self.spec.market_id,
                                    )
                                )
                        finally:
                            if heartbeat_task is not None:
                                heartbeat_task.cancel()
                                await asyncio.gather(
                                    heartbeat_task, return_exceptions=True
                                )
                except asyncio.CancelledError:
                    final_status = "CANCELLED"
                    raise
                except (ConnectionClosed, OSError, TimeoutError) as exc:
                    attempt += 1
                    delay = min(
                        self.reconnect_max_seconds,
                        2 ** min(attempt, 6),
                    )
                    delay *= random.uniform(0.8, 1.2)
                    self.logger.warning(
                        "%s desconectado: %s; reconexión en %.1fs",
                        self.spec.component,
                        exc,
                        delay,
                    )
                    await self._record_system(
                        "collector_disconnected",
                        {
                            "component": self.spec.component,
                            "error": str(exc),
                            "retry_seconds": round(delay, 3),
                        },
                    )
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                except Exception as exc:
                    final_status = "FAILED"
                    final_error = f"{type(exc).__name__}: {exc}"
                    await self._record_system(
                        "collector_failed",
                        {
                            "component": self.spec.component,
                            "error": final_error,
                        },
                    )
                    raise
        finally:
            await asyncio.to_thread(
                self.store.finish_run,
                run_id,
                final_status,
                final_error,
            )

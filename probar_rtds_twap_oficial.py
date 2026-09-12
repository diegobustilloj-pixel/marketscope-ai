from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path

from websockets.asyncio.client import connect

WS_URL = "wss://ws-live-data.polymarket.com"
DURATION_SECONDS = 90
OUTPUT = Path("data") / "rtds_probe_twap_oficial.jsonl"

SUBSCRIPTION = {
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices_twap_thirty",
            "type": "update",
            "filters": "{\"symbol\":\"btc/usd\"}",
        },
        {
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": "{\"symbol\":\"btc/usd\"}",
        },
    ],
}


async def heartbeat(ws, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await ws.send("PING")
        except Exception:
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        OUTPUT.unlink()

    deadline = time.monotonic() + DURATION_SECONDS
    counts: Counter[str] = Counter()
    twap_examples = []
    stop_event = asyncio.Event()

    print("=" * 68)
    print("PRUEBA OFICIAL RTDS TWAP - BTC/USD 30 SEGUNDOS")
    print("=" * 68)
    print(f"Duracion: {DURATION_SECONDS} segundos")
    print(f"Salida:   {OUTPUT}")
    print("Sin wallet, sin ordenes y sin dinero real.")
    print()

    async with connect(
        WS_URL,
        open_timeout=20,
        close_timeout=10,
        ping_interval=None,
        max_size=4 * 1024 * 1024,
    ) as ws:
        await ws.send(json.dumps(SUBSCRIPTION, separators=(",", ":")))
        hb = asyncio.create_task(heartbeat(ws, stop_event))
        try:
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(10.0, remaining))
                except asyncio.TimeoutError:
                    continue

                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")

                # Ignore heartbeat replies but preserve all JSON messages.
                if raw.strip().upper() in {"PONG", "PING"}:
                    counts["heartbeat"] += 1
                    continue

                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    counts["non_json"] += 1
                    print("NO JSON:", raw[:300])
                    continue

                topic = str(obj.get("topic") or "NO_TOPIC")
                msg_type = str(obj.get("type") or "NO_TYPE")
                counts[f"{topic}:{msg_type}"] += 1

                with OUTPUT.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

                if topic == "crypto_prices_twap_thirty":
                    payload = obj.get("payload")
                    if isinstance(payload, dict):
                        twap_examples.append(
                            {
                                "symbol": payload.get("symbol"),
                                "timestamp": payload.get("timestamp"),
                                "value": payload.get("value"),
                                "full_accuracy_value": payload.get("full_accuracy_value"),
                                "window_s": payload.get("window_s"),
                            }
                        )
                        if len(twap_examples) == 1:
                            print("PRIMER TWAP RECIBIDO:")
                            print(json.dumps(twap_examples[0], indent=2, ensure_ascii=False))
                            print()

                if topic == "NO_TOPIC":
                    print("MENSAJE SIN TOPIC:", json.dumps(obj, ensure_ascii=False)[:500])
        finally:
            stop_event.set()
            try:
                await hb
            except Exception:
                pass

    print()
    print("=" * 68)
    print("RESUMEN")
    print("=" * 68)
    for key, value in sorted(counts.items()):
        print(f"{key}: {value}")

    twap_count = sum(
        value for key, value in counts.items()
        if key.startswith("crypto_prices_twap_thirty:")
    )
    print()
    if twap_count > 0:
        print(f"TWAP 30s recibido correctamente: SI ({twap_count} mensajes)")
        print("La ruta oficial para Fase 4.2 queda confirmada.")
    else:
        print("TWAP 30s recibido correctamente: NO")
        print("No modifique aun el bot principal. Envie el JSONL y esta salida para diagnostico.")


if __name__ == "__main__":
    asyncio.run(main())

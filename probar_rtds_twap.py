from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path

from websockets.asyncio.client import connect

from polymarket_bot.config import Settings

DURATION_SECONDS = 90
OUTPUT = Path('data/rtds_probe_twap.jsonl')


def compact_payload(obj):
    if not isinstance(obj, dict):
        return obj
    keep = {}
    for key in (
        'symbol', 'timestamp', 'value', 'price', 'twap', 'window',
        'windowSeconds', 'startTimestamp', 'endTimestamp', 'feedId',
        'streamId', 'type', 'source'
    ):
        if key in obj:
            keep[key] = obj[key]
    if not keep:
        keep = {k: obj[k] for k in list(obj)[:12]}
    return keep


async def main() -> None:
    settings = Settings.from_env()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    payload_shapes = Counter()
    deadline = time.monotonic() + DURATION_SECONDS

    subscription = {
        'action': 'subscribe',
        'subscriptions': [
            {
                'topic': 'crypto_prices_chainlink',
                'type': '*',
                'filters': json.dumps({'symbol': 'btc/usd'}, separators=(',', ':')),
            }
        ],
    }

    print('RTDS/TWAP probe: 90 segundos, BTC/USD, sin wallet y sin ordenes.')
    print(f'Guardando payloads en: {OUTPUT.resolve()}')

    with OUTPUT.open('w', encoding='utf-8') as fh:
        async with connect(
            settings.rtds_ws_url,
            ping_interval=None,
            open_timeout=12,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
            max_queue=4096,
            proxy=True if settings.ws_use_proxy else None,
        ) as ws:
            await ws.send(json.dumps(subscription, separators=(',', ':')))
            next_ping = time.monotonic() + 5
            shown = 0
            while time.monotonic() < deadline:
                timeout = min(5.0, max(0.1, deadline - time.monotonic()))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except TimeoutError:
                    raw = None

                now = time.monotonic()
                if now >= next_ping:
                    await ws.send('PING')
                    next_ping = now + 5

                if raw is None:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8', errors='replace')
                fh.write(raw + '\n')
                fh.flush()

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    counts['MALFORMED'] += 1
                    continue

                if not isinstance(msg, dict):
                    counts['NON_OBJECT'] += 1
                    continue

                topic = str(msg.get('topic') or '')
                msg_type = str(msg.get('type') or '')
                key = f'{topic}|{msg_type}'
                counts[key] += 1
                payload = msg.get('payload')
                if isinstance(payload, dict):
                    payload_shapes[','.join(sorted(payload.keys()))] += 1

                if shown < 20:
                    print(json.dumps({
                        'topic': topic,
                        'type': msg_type,
                        'timestamp': msg.get('timestamp'),
                        'payload': compact_payload(payload),
                    }, ensure_ascii=False))
                    shown += 1

    print('\nRESUMEN TIPOS')
    for key, value in counts.most_common():
        print(f'{key}: {value}')
    print('\nRESUMEN FORMAS DE PAYLOAD')
    for key, value in payload_shapes.most_common(12):
        print(f'{value}x -> {key}')
    print('\nProbe finalizado. No se realizaron operaciones.')


if __name__ == '__main__':
    asyncio.run(main())

import sqlite3
import zlib
import re
import bisect
import datetime

DB = r"C:\ProyectoBotV3\polymarket_quant_bot\data\polymarket_soak_72h_v3.db"

print("Abriendo base en SOLO LECTURA...")
c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)

pat = re.compile(rb'"source_timestamp_ms":\s*(\d+)')
timestamps = []

print("Leyendo Chainlink historico...")

for (blob,) in c.execute("SELECT payload_blob FROM raw_chunks ORDER BY first_sequence"):
    data = zlib.decompress(blob)

    for line in data.splitlines():
        if b"crypto_prices_chainlink" not in line:
            continue

        m = pat.search(line)
        if m:
            timestamps.append(int(m.group(1)))

timestamps = sorted(set(timestamps))

markets = [
    row
    for row in c.execute("SELECT slug, end_at FROM markets ORDER BY end_at")
    if row[0] and row[0].startswith("btc-updown-5m-")
]

print()
print("BTC_5M_MARKETS:", len(markets))
print("CHAINLINK_TICKS:", len(timestamps))

for horizon in [120, 60, 30, 15]:
    fresh = 0
    ages = []

    for slug, end_at in markets:
        dt = datetime.datetime.fromisoformat(
            end_at.replace("Z", "+00:00")
        ) - datetime.timedelta(seconds=horizon)

        decision_ms = int(dt.timestamp() * 1000)

        i = bisect.bisect_right(timestamps, decision_ms) - 1

        if i >= 0:
            age = decision_ms - timestamps[i]

            if 0 <= age <= 5000:
                fresh += 1
                ages.append(age)

    pct = (fresh / len(markets) * 100) if markets else 0

    print(
        f"HORIZON {horizon}s FRESH: "
        f"{fresh}/{len(markets)} = {pct:.2f}% "
        f"MAX_AGE_MS: {max(ages) if ages else None}"
    )

gaps = [
    (a, b, b - a)
    for a, b in zip(timestamps, timestamps[1:])
    if b - a > 30000
]

print()
print("GAPS_GT_30S:", len(gaps))

for a, b, gap in gaps:
    print(
        datetime.datetime.fromtimestamp(a / 1000, datetime.UTC).isoformat(),
        "->",
        datetime.datetime.fromtimestamp(b / 1000, datetime.UTC).isoformat(),
        "GAP_S=",
        gap / 1000,
    )

c.close()
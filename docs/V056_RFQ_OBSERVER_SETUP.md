# V0.56 bounded authenticated RFQ observer

V0.56 is a new one-hour, authenticated, receive-only RFQ observation. It does
not modify or resume V0.55. It never submits quotes, confirmations, orders,
signatures, or transactions, and it never measures PnL.

## Why V0.56 exists

V0.55 authenticated successfully and observed 138,972 RFQ requests and 79 RFQ
trades, but its full row-per-request database reached the frozen 128 MiB total
footprint limit after about 12 minutes and 44 seconds. The generic reconnect
branch then retried a local storage failure ten times.

V0.56 replaces full persistence of every request with:

- exact counts for every message;
- exact request aggregates by minute, direction, side, leg count, and size unit;
- exact deadline totals and a 25 ms headroom histogram;
- exact distinct condition IDs stored as 32-byte values;
- all observed trade/auth/error records;
- a deterministic 1/16 request-detail sample, capped at 100,000 rows;
- commits in batches of 512 events and a bounded WAL;
- a 256 MiB total database limit that terminates explicitly without reconnecting;
- public `/time` samples at the beginning and end to bound server/local clock offset.

The official RFQ documentation defines `submission_deadline` as an epoch
millisecond value. The official CLOB `/time` endpoint returns Unix seconds, so
V0.56 stores interval bounds instead of pretending that the clock measurement
has millisecond precision.
The frozen maximum accepted interval width is 750 ms; this remains below the
endpoint's one-second resolution and accommodates the measured network latency.

## Files

- Preregistration: `data/prereg_v056_bounded_rfq_observer.json`
- Capture: `data/v056_bounded_rfq_observer.db`
- Final result: `data/resultado_v056_bounded_rfq_observer.json`

All three paths are new. Existing V0.55 artifacts are read-only evidence.

## Commands

Prepare exactly once, before credentials or network capture:

```powershell
.\.venv\Scripts\python.exe v056_monitor.py --prepare
```

Check the credential gate without printing values:

```powershell
.\.venv\Scripts\python.exe v056_monitor.py --preflight
```

For a Phantom legacy Safe account, use the local bootstrap. It obtains CLOB
credentials directly in Chrome and passes them only through the child process
environment:

```powershell
.\.venv\Scripts\python.exe v056_phantom_bootstrap.py
```

Read status without changing the capture:

```powershell
.\.venv\Scripts\python.exe v056_monitor.py --status
```

Run the audit exactly once only after a terminal status:

```powershell
.\.venv\Scripts\python.exe v056_monitor.py --audit
```

## Safety invariants

- `real_money=BLOQUEADO`
- `orders_created=0`
- `paper_orders=0`
- `quotes_submitted=0`
- `confirmations_sent=0`
- `transactions_created=0`
- no wallet private key required
- no credentials, wallet addresses, or raw payloads persisted
- no automatic follow-up run and no scheduled supervision

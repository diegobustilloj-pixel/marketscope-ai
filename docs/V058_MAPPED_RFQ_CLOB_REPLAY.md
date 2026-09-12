# V0.58 — mapped RFQ+CLOB paper replay

V0.58 corrects one identified technical error in V0.57 without changing the economic
hypothesis. RFQ `leg_position_ids` are Combo position IDs, not CLOB token IDs. V0.58
first derives a sanitized seed of the 6,518 distinct position IDs captured by frozen
V0.57 evidence. Before connecting to the RFQ stream, it queries the official Gamma
`markets/keyset` endpoint in bounded 50-position batches for open, Combo-enabled markets
and aligns each `positionIds` entry with the same-index entry in `clobTokenIds`.
Conflicts, an undersized map, or low seed resolution fail closed. Transient transport,
throttling, or server errors receive at most three attempts per batch with fixed
backoff, all before the RFQ connection begins. This avoids an unbounded scan of more
than 20,000 enabled markets while keeping mapping work outside the 400 ms request path.
The terminal technical gates also require at least 90% mapping coverage among structurally
eligible live requests, so the historical seed cannot silently bias the economic result.

Only sanitized position-to-token mappings are persisted. Raw Gamma payloads, requestor
identities, credentials, wallet addresses, and raw CLOB or RFQ messages are not stored.
Gamma receives no CLOB credentials.

The capture remains one hour and paper-only. It considers BUY/YES notional requests
with two to eight fully mapped legs. One of every eight requests is selected
deterministically by hashed RFQ id. This lowers load after V0.57 showed that a one-in-four
sample could fill the queue under a higher request burst. Each selected request receives
one unauthenticated `POST /books` call using the resolved CLOB token IDs.

The terminal economic replay is unchanged: compare the observed RFQ trade price, improved
by one micro-dollar, against an exact-depth purchase of the cheapest single desired leg,
including official CLOB taker fees. A book must complete within 400 ms of RFQ receipt and
carry a source timestamp no later than the submission deadline.

Any positive candidate remains counterfactual viability, not realized PnL or fill
probability. The system sends only RFQ authentication and never sends a quote, order,
confirmation, signature, or transaction. Real money remains blocked and there is no
scheduled supervision or automatic next experiment.

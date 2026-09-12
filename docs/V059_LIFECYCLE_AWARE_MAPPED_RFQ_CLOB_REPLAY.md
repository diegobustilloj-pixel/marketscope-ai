# V0.59 — lifecycle-aware mapped RFQ+CLOB paper replay

V0.59 is a separate preregistered correction for the historical-seed drift that safely
blocked V0.58 before database creation. It does not modify V0.58 or its evidence.

The V0.57 seed contains 6,518 public position IDs observed at an earlier time. Some of
those markets naturally close as time passes. A diagnostic showed that a missing open
position resolves under `closed=true`, proving lifecycle drift rather than corrupt IDs.
Closed markets cannot provide executable current books and are intentionally excluded.

Before connecting to RFQ, V0.59 resolves only the currently open, Combo-enabled subset
through Gamma `markets/keyset`, in bounded 50-position batches with three attempts per
batch. The historical-seed readiness gate is 70%, below the observed 75.05% open share.
This gate only protects against a grossly stale seed; it is not the economic validity gate.

The decisive mapping gate remains unchanged: at terminal audit, at least 90% of
structurally eligible live RFQ requests must map. Thus expired historical positions do not
block the capture, while new or missing active positions cannot silently bias its result.
Gamma receives no credentials and the map is built outside the 400 ms RFQ window.

The economic hypothesis and observation settings are unchanged from V0.58: one-hour
paper-only capture, BUY/YES notional requests with two to eight legs, deterministic
one-in-eight sampling, 20 public CLOB book calls per second, exact depth, and the same
single-leg superhedge counterfactual. V0.59 sends only RFQ authentication. It never sends
a quote, order, confirmation, operational signature, or transaction. Real money remains
blocked and no automatic follow-up is launched.

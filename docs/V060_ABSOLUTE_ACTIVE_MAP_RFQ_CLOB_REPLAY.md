# V0.60 — absolute active-map RFQ+CLOB paper replay

V0.60 is a separate preregistered correction for the time-dependent readiness gate that
blocked V0.59 before database creation. It does not modify V0.58, V0.59, or their evidence.

V0.59 proved that the percentage of the historical V0.57 position seed still open is not
a stable readiness criterion. It fell from 75.05% to 68.41% as markets closed, while all
131 Gamma batches completed without a retry. That percentage remains recorded as a
diagnostic but is not a V0.60 pass/fail gate.

Before RFQ connection, V0.60 still resolves the currently open, Combo-enabled subset of
the frozen 6,518-position seed through Gamma. It now requires three absolute minimums:
1,000 active markets, 2,000 mapped position records, and 1,000 resolved seed positions.
The last measured universe contained 3,623 markets, 7,228 mapping records, and 4,459
resolved seed positions. These checks detect a small, malformed, or grossly obsolete map
without tying readiness to a denominator that must decay over time.

The decisive terminal validity gate remains unchanged: at least 90% of structurally
eligible live RFQ requests must map, at least 10,000 mapped BUY/YES requests must be
observed, and all other V0.59 technical and sample gates must pass. Thus the absolute
preflight check cannot manufacture an economic PASS from an incomplete live map.

Economics, one-in-eight sampling, one-hour duration, CLOB rate limits, storage format,
and the single-leg superhedge counterfactual are unchanged. V0.60 sends only RFQ
authentication. It cannot send quotes, orders, confirmations, operational signatures, or
transactions. Real money remains blocked.

The local Phantom assistant reports `PREPARANDO` while the public map is being built.
It reports that the one-hour capture started only after the database exists and the run is
in `RUNNING` state. This prevents credential preflight from being mistaken for capture
start.

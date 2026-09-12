# V0.57 — RFQ+CLOB joined paper replay

V0.57 is a one-hour, preregistered feasibility test. It listens to the authenticated
RFQ feed, but its RFQ outbound allowlist contains only the authentication message.
It never constructs or submits a quote, confirmation, order, signature, or transaction.

The capture selects only BUY/YES requests with notional size and two to eight legs.
One request out of four is selected deterministically from the SHA-256 digest of its
RFQ id. For each selected request, the observer makes one unauthenticated public batch
request to `POST /books` and stores only compressed, sanitized ask depth and timestamps.
All RFQ trade broadcasts are stored with hashed RFQ ids so the terminal auditor can
join them without retaining the raw requestor identity.

The economic hypothesis is deliberately narrow. A maker short one combo YES share can
superhedge the conjunction payoff by owning one share of any desired leg, because the
product of binary leg payoffs cannot exceed any individual leg payoff. The terminal
auditor therefore finds the cheapest exact-depth single-leg purchase, including the
official taker-fee schedule, and compares it with an observed RFQ trade price improved
by one micro-dollar. The book must have completed within 400 ms of receipt and its
source timestamp must not exceed the RFQ submission deadline.

A positive candidate is not realized PnL. The feed does not reveal the competing
maker's allocation, and V0.57 did not actually quote, get selected, or execute. A pass
only permits a separate four-hour confirmatory paper design. Failure closes or redesigns
this exact mechanism.

Safety is frozen: credentials exist only in the child process environment, no private
key is used, no secrets or addresses are stored, real money is blocked, and there is no
scheduled supervision or automatic follow-up launch.

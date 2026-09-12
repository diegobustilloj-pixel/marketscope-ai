# PREREG — CLIMATE MANUAL STRATEGY V001

Frozen before the bulk outcome download on 2026-09-03.

## Purpose

Evaluate a receive-only, semi-automatic climate strategy. The scanner may recommend an entry, but the user must verify the resolution contract and place a limit order manually. No wallet integration and no automatic orders are allowed.

## Frozen forward sample

- Forecast, probability and executable-book snapshots: `data/climate_shadow_forward_v001.db`.
- Target markets: D+1 daily maximum/minimum markets dated 2026-09-03.
- Model selection was frozen before TEST under SHA-256 `a41ee83c94bc8f16b07de5a0eb5e0851d168a4f1994ebfe54d0b3b89934b64d1`.
- Confirmatory outcomes must be officially resolved by Gamma. Market-consensus prices are diagnostic only.

## Discovery-exposed exclusions

These outcomes were inspected while validating the resolver and are excluded from any confirmatory performance claim:

- `lowest-temperature-in-madrid-on-september-3-2026`
- `highest-temperature-in-jeddah-on-september-3-2026`
- `highest-temperature-in-shanghai-on-september-3-2026`

## Primary strategy — MANUAL_TOP_YES_V001

One entry maximum per event. Never flip side and never stack thresholds.

1. Use the first complete snapshot at or after 15:00 station-local time on the calendar day before the target date, with a maximum delay of 70 minutes.
2. Use only D+1 predictions and market families graded `STRONG`.
3. Require station TRAIN resolution reconstruction match rate >= 98% with at least 30 comparable events.
4. Trade only `YES` on the model's highest-probability bucket.
5. Require model probability >= 30%.
6. Require conservative gross edge >= 5 percentage points after adding 1 cent adverse price impact.
7. Require executable entry price between 0.10 and 0.75.
8. Require spread <= 0.10 and at least $25 visible notional at the best ask.
9. Simulated stake: $25 per event. Entry price is `best_ask + 0.01`, capped at 0.99.
10. Hold to resolution. Binary payout is $1 for the winning side and $0 otherwise.

Fees are not assumed to be zero for a real recommendation. If the fee status cannot be verified, the market is manual-review-only and cannot graduate to real money.

## Metrics and gates

Report trades, wins, losses, win rate, cost, PnL, ROI on cost, EV/trade, profit factor and chronological max drawdown.

The strategy may be called provisionally profitable only if all are true on non-exposed, officially resolved events:

- at least 30 independent event-level trades;
- positive net PnL and ROI;
- profit factor >= 1.25;
- lower 95% bootstrap bound for mean PnL/trade > 0;
- no unresolved outcome included;
- no contract or fee assumption silently treated as verified.

Otherwise the mandatory verdict is `MORE DATA REQUIRED` or `NO-GO`.

## Exploratory diagnostics

Threshold/timing variants may be reported only as exploratory and may not replace the frozen primary result. Provisional 0.9995/0.0005 market-consensus outcomes must be labeled separately from official resolutions.

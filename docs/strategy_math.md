# Strategy Math

## Momentum score

Only completed adjusted closes are accepted. For each day, calculate asset and
parking returns from adjacent adjusted closes, then subtract the parking return
from the asset return to get the daily excess return.

For each lookback `k` in 20, 60, and 120 sessions:

```text
ER_k = product(1 + asset_return) - product(1 + parking_return)
sigma_k = sample_std(daily_excess_returns, ddof=1)
M_k = ER_k / max(sigma_k * sqrt(k), min_volatility_floor)
score = 0.4 * M_20 + 0.4 * M_60 + 0.2 * M_120
```

The volatility floor protects the denominator; low or zero volatility does not
silently remove a candidate. The configured floor defaults to `0.000001`.
Candidate ties are resolved by descending score, descending `ER_120`,
descending 20-session average turnover, and ascending ticker.

## Candidate eligibility

Risk candidates pass these checks in order: active and valid spot-ETF status;
at least 200 quality-checked adjusted closes; positive momentum; latest
completed adjusted close above the 120-session adjusted-close average; at
least KRW 5 billion in average 20-session turnover and order size no greater
than 1% of that turnover; and no `blocked_until_next_rotation` restriction.
An existing holding remains rankable when it fails only the liquidity check,
but additional buying is blocked.

## Weekly portfolio calculation

`build_weekly_preview` is a deterministic calculation over the completed
candle date and ranked candidates. It applies the 45/30/20 slot weights and
region/index concentration limits, fills unused risk slots with the configured
parking ticker, and reports only ticker-level buys and sells. Live resolution
skips candidates that fail the first-open-day safety gate and promotes the
next eligible ranked candidates.

Smart DCA subtracts pending buy value from the target deficit, preserves at
least 5% of account equity as cash, applies the minimum KRW 150,000 trade
threshold, and uses the broker's latest available-cash value without
subtracting reserved orders a second time.

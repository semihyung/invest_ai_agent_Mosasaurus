# Risk Modes

Mode priority is SAFE_HALT, EMERGENCY, PAUSED, TRAINING, then NORMAL. SAFE_HALT blocks every new order. EMERGENCY and PAUSED allow only validated REDUCE_ONLY sells. TRAINING allows parking buys and stop enforcement.

## Permission rules

| Effective mode | Risk-asset buy | Smart DCA | Parking buy | Reduce-only sell |
| --- | --- | --- | --- | --- |
| SAFE_HALT | Blocked | Blocked | Blocked | Blocked |
| EMERGENCY | Blocked | Blocked | Blocked | Only after a hard-stop signal and all safety gates pass |
| PAUSED | Blocked | Blocked | Blocked | Only after a hard-stop signal and all safety gates pass |
| TRAINING | Blocked | Blocked | Allowed | Only after a hard-stop signal and all safety gates pass |
| NORMAL | Allowed in NORMAL/CAUTION drawdown | Allowed only in NORMAL drawdown | Allowed outside CRITICAL drawdown | Only after a hard-stop signal and all safety gates pass |

Drawdown thresholds are inclusive: `-7%` enters CAUTION, `-10%` enters
WARNING, and `-15%` enters CRITICAL. CRITICAL resolves to EMERGENCY. WARNING
blocks risk-asset buys while retaining parking buys; CRITICAL blocks all buys.
Queries, reconciliation, and cancellation remain available in every mode.

## Flow-adjusted performance

Performance is initialized at unit NAV `100` once starting equity is positive.
DEPOSIT and WITHDRAWAL adjust unit count at the unit NAV at their verified
`occurred_at` time. DISTRIBUTION does not adjust unit count, so its value is
captured as investment return on the next equity valuation. Unknown, missing,
or out-of-order cash-flow data marks TWR quality `DATA_INVALID`; the prior HWM
is preserved and performance updates are blocked until reconciliation.

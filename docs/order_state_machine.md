# Order State Machine

The 11 KIS states are `CREATED`, `SUBMITTING`, `PENDING_CONFIRMATION`,
`ACCEPTED`, `PARTIALLY_FILLED`, `CANCEL_PENDING`, `FILLED`, `CANCELED`,
`REJECTED`, `CONFIRMED_NOT_ACCEPTED`, and `EXPIRED`. Terminal states are
immutable. `CONFIRMED_NOT_ACCEPTED` is the only state from which a new attempt
may be recorded, and each attempt has a new `attempt_id` while retaining the
same parent intent.

## Idempotent intents

The idempotency key is a SHA-256 digest of `cycle_id`, `intent_type`, `slot_id`,
ticker, and side. It deliberately excludes run ID, quantity, price, and attempt
ID. The policy payload hash includes the original requested quantity and price
policy; a replay with the same key but a different payload raises
`INTENT_CONFLICT`. SQLite `PRIMARY KEY`/`UNIQUE` constraints provide a second
guard.

## Timeout recovery

A submission timeout transitions to `PENDING_CONFIRMATION` and is never
retransmitted automatically. One or more missing lookup results are not proof
of non-acceptance. Recovery remains pending until explicit rejection or both
the authoritative order query and execution query are complete and the broker
processing-delay window has elapsed. Only then can it transition to
`CONFIRMED_NOT_ACCEPTED` and permit a newly recorded attempt.

## Cancellation and exit obligations

At the configured `15:14:30 KST` cutoff, enumerate all accepted or partially
filled orders with remaining quantity for cancellation. Do not treat a cancel
request as a confirmed cancellation; reconcile fills and broker status.
Unresolved sell quantity is persisted in `exit_obligations` as `OPEN`. The
next-session checkpoint may return remaining quantity only after position
reconciliation, terminal status of prior orders, fresh account data, and valid
quote/corporate-action data all pass. Otherwise the obligation remains open.

`SAFE_HALT` permits reads, reconciliation, and cancellation only. All other
new submissions pass through the order manager's common permission and REAL
gates; order types are restricted to `LIMIT`.

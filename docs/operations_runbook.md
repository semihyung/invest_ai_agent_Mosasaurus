# Operations Runbook

On KIS, network, UPS, or database failure: stop new orders, preserve PENDING_CONFIRMATION and exit obligations, reconcile KIS and SQLite, and require operator clearance before returning to NORMAL.

## Scheduler Fail-Closed Behavior

The scheduler registers the Phase 1 KST jobs with the grace periods defined in
the specification. Order-related jobs do not run while the market is closed or
while `scheduler_order_lock` is absent, true, or invalid. A missed or failed
order-related job records a sanitized `system_events` row and latches that
lock. Clear it only after a successful reconciliation and confirmation that
`is_safe_halt` is not true; missed work is never replayed as a catch-up order.

On startup, `boot_recovery` keeps order jobs locked until database integrity,
token validity, and account reconciliation pass. A pre-existing `SAFE_HALT` is
never cleared automatically. Telegram confirmation requests are written to
`pending_actions`; requests from a prior process are expired rather than
re-associated with a chat after restart. If a Telegram polling process crashes,
verify that it is no longer running before clearing `telegram_polling_lock`.

Database backups are accompanied by SHA-256 files. `restore_test.sh` verifies
the checksum and tests a temporary copy; it does not overwrite the live
database. The GitHub `production` environment must be configured with required
reviewers before enabling the self-hosted deploy runner. Shell scripts and
hardware-dependent health/watchdog behavior still require Linux/Raspberry Pi
validation.

## Universe Classification Gate

`config/universe_allowlist.yaml` is the operator-reviewed seed list. The checked-in
file is intentionally empty; do not add a ticker until its KRX/KIS structured
product metadata and operator approval have been verified. Startup imports only
approved entries and refuses to overwrite a database classification that differs
from the file. Existing assets upgraded from schema v1 remain unverified and
cannot enter previews or new-buy candidates.

Each `assets` entry must provide `ticker`, `name`, `category`, `region`,
`underlying_index`, `product_type`, `replication_method`, `leverage_multiplier`,
`is_inverse`, `is_futures_based`, `classification_source`, `verified_by`, and
timezone-aware `verified_at`. Use only the exact official source values accepted
by the validator; placeholder records are not valid approvals.

Telegram `/add` requires an injected official product-metadata lookup. When that
provider is not configured, the command is rejected rather than relying on
product-name keywords or approving an unverified ticker. After confirmation,
the product metadata and masked operator identity are stored with the asset.

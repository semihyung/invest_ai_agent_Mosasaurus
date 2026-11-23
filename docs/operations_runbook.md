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

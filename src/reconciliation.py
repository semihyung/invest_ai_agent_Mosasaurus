from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    ticker: str
    quantity: int


@dataclass(frozen=True)
class ReconciliationResult:
    reconciliation_ok: bool
    mismatches: tuple[str, ...]


def reconcile_positions(
    broker_positions: list[Position],
    sqlite_positions: list[Position],
) -> ReconciliationResult:
    broker = {position.ticker: position.quantity for position in broker_positions}
    sqlite = {position.ticker: position.quantity for position in sqlite_positions}
    mismatches = []

    for ticker in sorted(set(broker) | set(sqlite)):
        if broker.get(ticker, 0) != sqlite.get(ticker, 0):
            mismatches.append(ticker)

    return ReconciliationResult(
        reconciliation_ok=not mismatches,
        mismatches=tuple(mismatches),
    )


def reconciliation_safe_halt(
    *,
    position_result: ReconciliationResult,
    unknown_order_exists: bool,
    stale_account_data: bool,
) -> bool:
    return (
        not position_result.reconciliation_ok
        or unknown_order_exists
        or stale_account_data
    )

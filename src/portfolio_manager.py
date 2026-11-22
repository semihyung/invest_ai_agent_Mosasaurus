from dataclasses import dataclass
from typing import Iterable


SLOT_WEIGHTS = {"S1": 0.45, "S2": 0.30, "S3": 0.20}


@dataclass(frozen=True)
class SlotCandidate:
    ticker: str
    region: str
    underlying_index: str


@dataclass(frozen=True)
class SlotAssignment:
    slot_id: str
    ticker: str
    target_weight: float
    reason: str


def assign_risk_slots(
    candidates: Iterable[SlotCandidate],
    *,
    max_slots_per_region: int = 2,
) -> list[SlotAssignment]:
    if isinstance(max_slots_per_region, bool) or max_slots_per_region < 1:
        raise ValueError("max_slots_per_region must be positive.")

    region_counts: dict[str, int] = {}
    underlying_indexes: set[str] = set()
    assignments: list[SlotAssignment] = []

    for candidate in candidates:
        if not isinstance(candidate, SlotCandidate):
            raise TypeError("Candidates must be SlotCandidate values.")
        if len(assignments) >= len(SLOT_WEIGHTS):
            break
        if candidate.region in region_counts and region_counts[candidate.region] >= max_slots_per_region:
            continue
        if candidate.underlying_index in underlying_indexes:
            continue

        slot_id = ("S1", "S2", "S3")[len(assignments)]
        assignments.append(
            SlotAssignment(
                slot_id=slot_id,
                ticker=candidate.ticker,
                target_weight=SLOT_WEIGHTS[slot_id],
                reason="ranked_candidate",
            )
        )
        region_counts[candidate.region] = region_counts.get(candidate.region, 0) + 1
        underlying_indexes.add(candidate.underlying_index)

    for slot_id in ("S1", "S2", "S3"):
        if any(assignment.slot_id == slot_id for assignment in assignments):
            continue
        assignments.append(
            SlotAssignment(
                slot_id=slot_id,
                ticker="",
                target_weight=SLOT_WEIGHTS[slot_id],
                reason="parking_fallback",
            )
        )
    return sorted(assignments, key=lambda assignment: assignment.slot_id)


def resolve_live_slots(
    preview_ranked_tickers: Iterable[str],
    live_eligible_tickers: Iterable[str],
    parking_ticker: str,
    existing_holdings: Iterable[str] = (),
) -> list[SlotAssignment]:
    preview = list(preview_ranked_tickers)
    eligible = set(live_eligible_tickers)
    holdings = set(existing_holdings)
    selected: list[str] = []

    for ticker in preview:
        if ticker in eligible and ticker not in selected:
            selected.append(ticker)
        if len(selected) == 3:
            break

    if not selected:
        for ticker in holdings:
            if ticker in eligible and ticker not in selected:
                selected.append(ticker)
            if len(selected) == 3:
                break

    assignments = []
    for index, slot_id in enumerate(("S1", "S2", "S3")):
        ticker = selected[index] if index < len(selected) else parking_ticker
        reason = "live_eligible" if index < len(selected) else "parking_fallback"
        assignments.append(
            SlotAssignment(
                slot_id=slot_id,
                ticker=ticker,
                target_weight=SLOT_WEIGHTS[slot_id],
                reason=reason,
            )
        )
    return assignments


def calculate_target_amounts(
    total_equity: int,
    parking_ticker: str,
) -> dict[str, float]:
    if isinstance(total_equity, bool) or total_equity < 0:
        raise ValueError("total_equity must be a non-negative integer.")
    parking_weight = 0.05
    targets = {
        slot_id: total_equity * weight
        for slot_id, weight in SLOT_WEIGHTS.items()
    }
    targets[parking_ticker] = total_equity * parking_weight
    return targets


def detect_external_deposit(
    current_cash: int,
    prior_cash: int,
    sell_fills: int,
    buy_fills: int,
    fees: int,
) -> int:
    net_external_flow = current_cash - (prior_cash + sell_fills - buy_fills - fees)
    return max(0, net_external_flow)


def calculate_smart_dca(
    cash_balance: int,
    total_equity: int,
    target_amount: int,
    current_amount: int,
    limit_price: int,
    available_cash: int,
    estimated_fee: int,
) -> int:
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for value in (
            cash_balance,
            total_equity,
            target_amount,
            current_amount,
            available_cash,
            estimated_fee,
        )
    ):
        raise ValueError("Cash and amount values must be non-negative integers.")
    if isinstance(limit_price, bool) or limit_price <= 0:
        raise ValueError("limit_price must be a positive integer.")

    if cash_balance <= 0.05 * total_equity + 150_000:
        return 0

    deficit = target_amount - current_amount
    if deficit <= 0:
        return 0

    cash_limit = available_cash - int(0.05 * total_equity) - estimated_fee
    if cash_limit <= 0:
        return 0
    return min(deficit // limit_price, cash_limit // limit_price)

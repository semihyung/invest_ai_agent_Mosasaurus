"""주간 슬롯 배정과 현금 보존형 포트폴리오 수량 계산을 담당합니다."""

import math
from datetime import date
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping


SLOT_WEIGHTS = {"S1": 0.45, "S2": 0.30, "S3": 0.20}


@dataclass(frozen=True)
class SlotCandidate:
    """순위와 집중도 검증에 쓰이는 위험자산 후보입니다."""

    ticker: str
    region: str
    underlying_index: str
    # v3.7.4: 헤지/비헤지·TR/PR 버전을 같은 지수로 묶는 정규화 식별자(비어 있으면 underlying_index 사용).
    underlying_index_family: str = ""


@dataclass(frozen=True)
class SlotAssignment:
    """포트폴리오 슬롯에 배정된 종목과 목표 비중입니다."""

    slot_id: str
    ticker: str
    target_weight: float
    reason: str


@dataclass(frozen=True)
class ProjectedTrade:
    """주간 프리뷰에서 예상한 매수 또는 매도 한 건입니다."""

    side: str
    slot_id: str
    ticker: str


@dataclass(frozen=True)
class WeeklyPreview:
    """토요일 보고 및 첫 개장일 검토에 사용할 순위·배정·예상 거래입니다."""

    base_candle_date: str
    ranked_candidates: tuple[SlotCandidate, ...]
    projected_slots: tuple[SlotAssignment, ...]
    projected_trades: tuple[ProjectedTrade, ...]


def assign_risk_slots(
    candidates: Iterable[SlotCandidate],
    *,
    max_slots_per_region: int = 2,
) -> list[SlotAssignment]:
    """후보 순위를 유지하며 지역·기초지수 집중도 한도 안에서 슬롯을 배정합니다."""
    if (
        isinstance(max_slots_per_region, bool)
        or not isinstance(max_slots_per_region, int)
        or max_slots_per_region < 1
    ):
        raise ValueError("max_slots_per_region must be a positive integer.")

    region_counts: dict[str, int] = {}
    underlying_indexes: set[str] = set()
    assigned_tickers: set[str] = set()
    assignments: list[SlotAssignment] = []

    for candidate in candidates:
        if not isinstance(candidate, SlotCandidate):
            raise TypeError("Candidates must be SlotCandidate values.")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                candidate.ticker,
                candidate.region,
                candidate.underlying_index,
            )
        ):
            raise ValueError("Candidate ticker, region, and index are required.")
        if len(assignments) >= len(SLOT_WEIGHTS):
            break
        if candidate.ticker in assigned_tickers:
            continue
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
        assigned_tickers.add(candidate.ticker)

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


DEFAULT_MAX_SLOTS_PER_REGION = {"KR": 2, "US": 2, "GLOBAL": 1}
DEFAULT_MAX_REGION_WEIGHT = {"KR": 0.75, "US": 0.65, "GLOBAL": 0.30}


def _family(candidate: SlotCandidate) -> str:
    """지수 패밀리가 없으면 기초지수 이름을 패밀리로 사용합니다."""
    return candidate.underlying_index_family or candidate.underlying_index


def _passes_concentration(
    candidate: SlotCandidate,
    selected: list[SlotCandidate],
    *,
    max_slots_per_region: Mapping[str, int],
    max_pairwise_correlation: float,
    correlation: Callable[[str, str], float | None] | None,
) -> bool:
    """지수 패밀리·지역 슬롯 수·60일 상관 중복 여부를 한 후보에 대해 검사합니다."""
    region_limit = max_slots_per_region.get(candidate.region)
    if region_limit is None:
        return False
    if sum(1 for item in selected if item.region == candidate.region) >= region_limit:
        return False
    for other in selected:
        if _family(other) == _family(candidate):
            return False
        if correlation is not None:
            value = correlation(candidate.ticker, other.ticker)
            # 분산 0 등으로 상관을 계산할 수 없으면(NaN/None) 중복으로 보지 않습니다.
            if (
                value is not None
                and math.isfinite(value)
                and value >= max_pairwise_correlation
            ):
                return False
    return True


def select_final_slots(
    ranked: Iterable[SlotCandidate],
    *,
    held_tickers: Iterable[str] = (),
    hold_rank_buffer: int = 5,
    max_slots_per_region: Mapping[str, int] | None = None,
    max_region_weight: Mapping[str, float] | None = None,
    max_pairwise_correlation: float = 0.90,
    correlation: Callable[[str, str], float | None] | None = None,
    slot_weights: Mapping[str, float] | None = None,
) -> list[SlotAssignment]:
    """명세 5.3절: 순위 버퍼·집중도 규칙으로 종목 집합을 정한 뒤 순위순으로 슬롯을 재배정합니다.

    ① 순위 `hold_rank_buffer` 이내 보유 종목을 먼저 담고 ② 남은 칸을 순위순으로 채웁니다.
    ③ 최종 집합은 순위순으로 S1~S3 에 다시 매겨 버퍼로 남은 약한 종목이 큰 비중을 차지하지
    않게 합니다. ④ 지역 비중 합이 한도를 넘으면 그 지역에서 가장 낮은 순위 종목을 제외하고
    다시 채웁니다(제외 목록이 늘어나므로 반드시 종료).
    """
    ordered = list(ranked)
    if any(not isinstance(item, SlotCandidate) for item in ordered):
        raise TypeError("Candidates must be SlotCandidate values.")
    if (
        isinstance(hold_rank_buffer, bool)
        or not isinstance(hold_rank_buffer, int)
        or hold_rank_buffer < 1
    ):
        raise ValueError("hold_rank_buffer must be a positive integer.")
    region_limits = dict(max_slots_per_region or DEFAULT_MAX_SLOTS_PER_REGION)
    region_weights = dict(max_region_weight or DEFAULT_MAX_REGION_WEIGHT)
    weights = dict(slot_weights or SLOT_WEIGHTS)
    slot_ids = ("S1", "S2", "S3")
    held = set(held_tickers)
    rank_of = {item.ticker: index + 1 for index, item in enumerate(ordered)}
    excluded: set[str] = set()

    for _ in range(len(ordered) + 1):
        selected: list[SlotCandidate] = []
        pool = [item for item in ordered if item.ticker not in excluded]
        buffered = [
            item for item in pool
            if item.ticker in held and rank_of[item.ticker] <= hold_rank_buffer
        ]
        for item in buffered + [x for x in pool if x not in buffered]:
            if len(selected) >= len(slot_ids):
                break
            if _passes_concentration(
                item,
                selected,
                max_slots_per_region=region_limits,
                max_pairwise_correlation=max_pairwise_correlation,
                correlation=correlation,
            ):
                selected.append(item)
        selected.sort(key=lambda item: rank_of[item.ticker])

        # 지역 비중 합 검사: 위반 시 그 지역의 최저 순위 종목을 제외하고 다시 시도합니다.
        violation = None
        for region, limit in region_weights.items():
            members = [
                (slot_ids[index], item)
                for index, item in enumerate(selected)
                if item.region == region
            ]
            if sum(weights[slot] for slot, _ in members) > limit + 1e-9:
                violation = members[-1][1]
                break
        if violation is None:
            break
        excluded.add(violation.ticker)

    assignments = [
        SlotAssignment(
            slot_id=slot_ids[index],
            ticker=item.ticker,
            target_weight=weights[slot_ids[index]],
            reason=(
                "rank_buffer_hold"
                if item.ticker in held and rank_of[item.ticker] > len(slot_ids)
                else "ranked_candidate"
            ),
        )
        for index, item in enumerate(selected)
    ]
    for slot_id in slot_ids[len(selected):]:
        assignments.append(
            SlotAssignment(slot_id, "", weights[slot_id], "parking_fallback")
        )
    return assignments


def build_weekly_preview(
    base_candle_date: str,
    ranked_candidates: Iterable[SlotCandidate],
    *,
    parking_ticker: str,
    existing_slot_tickers: Mapping[str, str] | None = None,
    max_slots_per_region: int = 2,
) -> WeeklyPreview:
    """완료 일봉 기준 주간 순위, 다음 슬롯과 종목 단위 예상 거래를 만듭니다."""
    if not isinstance(base_candle_date, str):
        raise ValueError("base_candle_date must be an ISO date string.")
    try:
        parsed_date = date.fromisoformat(base_candle_date)
    except ValueError as exc:
        raise ValueError("base_candle_date must be an ISO date string.") from exc
    if parsed_date.isoformat() != base_candle_date:
        raise ValueError("base_candle_date must be an ISO date string.")
    if not isinstance(parking_ticker, str) or not parking_ticker.strip():
        raise ValueError("parking_ticker must be a non-empty string.")

    ranked = tuple(ranked_candidates)
    if any(not isinstance(candidate, SlotCandidate) for candidate in ranked):
        raise TypeError("Candidates must be SlotCandidate values.")
    if any(candidate.ticker == parking_ticker for candidate in ranked):
        raise ValueError("The parking ticker cannot be a risk-slot candidate.")

    if existing_slot_tickers is not None and not isinstance(
        existing_slot_tickers,
        Mapping,
    ):
        raise TypeError("existing_slot_tickers must be a mapping.")
    previous_slots = {} if existing_slot_tickers is None else dict(existing_slot_tickers)
    valid_slot_ids = set(SLOT_WEIGHTS)
    if any(slot_id not in valid_slot_ids for slot_id in previous_slots):
        raise ValueError("Existing slots must use S1, S2, or S3.")
    if any(
        not isinstance(ticker, str) or not ticker.strip()
        for ticker in previous_slots.values()
    ):
        raise ValueError("Existing slot tickers must be non-empty strings.")

    projected = assign_risk_slots(
        ranked,
        max_slots_per_region=max_slots_per_region,
    )
    # 위험 후보가 모자란 슬롯은 지정된 대표 파킹 자산으로 채웁니다.
    projected = [
        SlotAssignment(
            slot_id=assignment.slot_id,
            ticker=assignment.ticker or parking_ticker,
            target_weight=assignment.target_weight,
            reason=(
                "parking_fallback"
                if assignment.reason == "parking_fallback"
                else assignment.reason
            ),
        )
        for assignment in projected
    ]

    next_tickers = {assignment.ticker for assignment in projected}
    held_tickers = set(previous_slots.values())
    trades = []
    for slot_id in ("S1", "S2", "S3"):
        ticker = previous_slots.get(slot_id)
        if (
            ticker is not None
            and ticker != parking_ticker
            and ticker not in next_tickers
        ):
            trades.append(ProjectedTrade("SELL", slot_id, ticker))
    trades.extend(
        ProjectedTrade("BUY", assignment.slot_id, assignment.ticker)
        for assignment in projected
        if assignment.ticker != parking_ticker
        and assignment.ticker not in held_tickers
    )
    return WeeklyPreview(
        base_candle_date=base_candle_date,
        ranked_candidates=ranked,
        projected_slots=tuple(projected),
        projected_trades=tuple(trades),
    )


def resolve_live_slots(
    preview_ranked_tickers: Iterable[str | SlotCandidate],
    live_eligible_tickers: Iterable[str],
    parking_ticker: str,
    existing_holdings: Iterable[str] = (),
    *,
    max_slots_per_region: int = 2,
) -> list[SlotAssignment]:
    """실시간 탈락 후보를 건너뛰고 순위 차순위를 승계해 최종 슬롯을 확정합니다."""
    if not isinstance(parking_ticker, str) or not parking_ticker.strip():
        raise ValueError("parking_ticker must be a non-empty string.")
    preview = list(preview_ranked_tickers)
    if (
        isinstance(max_slots_per_region, bool)
        or not isinstance(max_slots_per_region, int)
        or max_slots_per_region < 1
    ):
        raise ValueError("max_slots_per_region must be a positive integer.")
    if preview and not (
        all(isinstance(item, str) for item in preview)
        or all(isinstance(item, SlotCandidate) for item in preview)
    ):
        raise ValueError("Preview ranking must contain only tickers or candidate records.")
    eligible = set(live_eligible_tickers)
    holdings = set(existing_holdings)
    preview_tickers = [
        item.ticker if isinstance(item, SlotCandidate) else item
        for item in preview
    ]
    if any(not isinstance(ticker, str) or not ticker for ticker in preview_tickers):
        raise ValueError("Preview tickers must be non-empty strings.")
    if any(not isinstance(ticker, str) or not ticker for ticker in eligible):
        raise ValueError("Eligible tickers must be non-empty strings.")
    if any(not isinstance(ticker, str) or not ticker for ticker in holdings):
        raise ValueError("Existing holding tickers must be non-empty strings.")
    selected: list[str] = []
    selected_regions: dict[str, int] = {}
    selected_indexes: set[str] = set()

    for item in preview:
        ticker = item.ticker if isinstance(item, SlotCandidate) else item
        if ticker != parking_ticker and ticker in eligible and ticker not in selected:
            if isinstance(item, SlotCandidate):
                if (
                    selected_regions.get(item.region, 0) >= max_slots_per_region
                    or item.underlying_index in selected_indexes
                ):
                    continue
                selected_regions[item.region] = (
                    selected_regions.get(item.region, 0) + 1
                )
                selected_indexes.add(item.underlying_index)
            selected.append(ticker)
        if len(selected) == 3:
            break

    if not selected:
        # 집합의 반복 순서는 비결정적이므로 티커 정렬로 복구 결과를 고정합니다.
        for ticker in sorted(holdings):
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
    """계좌 평가액을 위험 슬롯과 남은 파킹 비중으로 나눕니다."""
    if (
        isinstance(total_equity, bool)
        or not isinstance(total_equity, int)
        or total_equity < 0
    ):
        raise ValueError("total_equity must be a non-negative integer.")
    if not isinstance(parking_ticker, str) or not parking_ticker.strip():
        raise ValueError("parking_ticker must be a non-empty string.")
    parking_weight = round(1.0 - sum(SLOT_WEIGHTS.values()), 10)
    if parking_ticker in SLOT_WEIGHTS:
        raise ValueError("parking_ticker cannot be a slot identifier.")
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
    """현금 잔액 변동에서 매매대금과 수수료를 제외한 순유입액을 구합니다."""
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
    *,
    pending_buy_amount: int = 0,
    min_trade_amount: int = 150_000,
) -> int:
    """목표 부족액·미체결 매수·현금 버퍼를 반영해 지정가 매수 수량을 계산합니다."""
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
            pending_buy_amount,
            min_trade_amount,
        )
    ):
        raise ValueError("Cash and amount values must be non-negative integers.")
    if min_trade_amount == 0:
        raise ValueError("min_trade_amount must be positive.")
    if (
        isinstance(limit_price, bool)
        or not isinstance(limit_price, int)
        or limit_price <= 0
    ):
        raise ValueError("limit_price must be a positive integer.")

    cash_buffer = (total_equity + 19) // 20
    if cash_balance <= cash_buffer:
        return 0

    # 미체결 매수액은 목표 초과 방지에만 반영하고, 매수가능금액에서 중복 차감하지 않습니다.
    target_gap = target_amount - current_amount
    if target_gap < min_trade_amount:
        return 0
    deficit = target_gap - pending_buy_amount
    if deficit <= 0:
        return 0

    cash_limit = available_cash - cash_buffer - estimated_fee
    if cash_limit <= 0:
        return 0
    return min(deficit // limit_price, cash_limit // limit_price)

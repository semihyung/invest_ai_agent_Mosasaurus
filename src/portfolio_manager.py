"""주간 슬롯 배정과 주간 프리뷰를 담당합니다(매수 수량은 daily_pipeline.buy_quantity)."""

import math
from datetime import date
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence


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
    slots: Sequence[SlotAssignment] | None = None,
) -> WeeklyPreview:
    """완료 일봉 기준 주간 순위, 다음 슬롯과 종목 단위 예상 거래를 만듭니다.

    `slots` 를 주면(`select_final_slots` 결과) 그대로 쓴다: 월요일 15:00 실행과 같은 규칙(지역·지역 비중·상관·순위
    버퍼)으로 계산한 슬롯이어야 주말 브리핑이 실제 집행과 어긋나지 않는다. 생략하면 단순 규칙(`assign_risk_slots`)을 쓴다.
    """
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

    projected = list(slots) if slots is not None else assign_risk_slots(
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

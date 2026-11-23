"""현금흐름을 단위화해 TWR과 유량 보정 HWM을 계산합니다."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math


class CashFlowType(str, Enum):
    """계좌 현금 변동을 성과 반영 방식에 따라 분류합니다."""

    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    DISTRIBUTION = "DISTRIBUTION"
    UNCLASSIFIED = "UNCLASSIFIED"


class CashFlowClassificationStatus(str, Enum):
    """브로커 현금흐름 원인이 확인되었는지 나타냅니다."""

    CONFIRMED = "CONFIRMED"
    UNCLASSIFIED_CASH_FLOW = "UNCLASSIFIED_CASH_FLOW"


class TWRQualityStatus(str, Enum):
    """성과 지수와 현금흐름의 계산 신뢰 상태입니다."""

    VALID = "VALID"
    DATA_INVALID = "DATA_INVALID"


@dataclass(frozen=True)
class CashFlowClassification:
    """현금흐름 종류와 분류 확인 결과를 함께 반환합니다."""

    flow_type: CashFlowType
    classification_status: CashFlowClassificationStatus

    @property
    def adjusts_units(self) -> bool:
        """입출금처럼 원금 흐름으로 단위 수를 바꾸는 종류인지 반환합니다."""
        return self.flow_type in (CashFlowType.DEPOSIT, CashFlowType.WITHDRAWAL)


@dataclass(frozen=True)
class PerformanceState:
    """기준 단위가격, 보유 단위 수, HWM 및 누적 성과 상태입니다."""

    unit_nav: float
    units: float
    hwm: float
    twr_cumulative: float
    drawdown: float
    as_of: datetime
    twr_quality_status: TWRQualityStatus = TWRQualityStatus.VALID

    def __post_init__(self) -> None:
        """성과 지수와 HWM이 유한한 값이며 정상 범위인지 확인합니다."""
        for value in (
            self.unit_nav,
            self.units,
            self.hwm,
            self.twr_cumulative,
            self.drawdown,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Performance state values must be finite numbers.")
        if (
            self.unit_nav < 0
            or self.units <= 0
            or self.hwm <= 0
            or self.hwm < self.unit_nav
            or not -1 <= self.drawdown <= 0
        ):
            raise ValueError("Performance state values are outside valid ranges.")
        if not isinstance(self.twr_quality_status, TWRQualityStatus):
            raise ValueError("twr_quality_status is invalid.")
        object.__setattr__(self, "as_of", _utc_datetime(self.as_of))


def _utc_datetime(value: datetime) -> datetime:
    """타임스탬프가 시간대 정보를 가진 값인지 확인하고 UTC로 맞춥니다."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Performance timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _finite_amount(value: float | None) -> bool:
    """금액이 숫자형의 유한 값인지 확인합니다."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _mark_data_invalid(state: PerformanceState) -> PerformanceState:
    """원인을 확인할 때까지 기존 HWM과 단위값을 보존한 채 매수 차단 상태로 둡니다."""
    return PerformanceState(
        unit_nav=state.unit_nav,
        units=state.units,
        hwm=state.hwm,
        twr_cumulative=state.twr_cumulative,
        drawdown=state.drawdown,
        as_of=state.as_of,
        twr_quality_status=TWRQualityStatus.DATA_INVALID,
    )


def classify_cash_flow(flow_type: str | CashFlowType) -> CashFlowClassification:
    """알려진 입출금·분배금을 구분하고 모르는 유형은 미분류로 표시합니다."""
    try:
        classified_type = CashFlowType(flow_type)
    except (TypeError, ValueError):
        classified_type = CashFlowType.UNCLASSIFIED
    status = (
        CashFlowClassificationStatus.UNCLASSIFIED_CASH_FLOW
        if classified_type is CashFlowType.UNCLASSIFIED
        else CashFlowClassificationStatus.CONFIRMED
    )
    return CashFlowClassification(classified_type, status)


def initialize_flow_adjusted_performance(
    *,
    starting_equity: float,
    as_of: datetime,
) -> PerformanceState | None:
    """최초 평가액이 양수일 때 단위가격 100으로 성과 지수를 시작합니다."""
    if (
        isinstance(starting_equity, bool)
        or not isinstance(starting_equity, (int, float))
        or not math.isfinite(starting_equity)
        or starting_equity < 0
    ):
        raise ValueError("Starting equity must be a finite non-negative amount.")
    if starting_equity == 0:
        return None

    timestamp = _utc_datetime(as_of)
    unit_nav = 100.0
    units = starting_equity / unit_nav
    return PerformanceState(
        unit_nav=unit_nav,
        units=units,
        hwm=unit_nav,
        twr_cumulative=0.0,
        drawdown=0.0,
        as_of=timestamp,
    )


def apply_cash_flow(
    state: PerformanceState,
    *,
    cash_flow: float | None,
    flow_type: str | CashFlowType,
    occurred_at: datetime | None,
) -> PerformanceState:
    """발생 시각에 입출금 단위 수를 조정하고 분배금은 성과에 남깁니다."""
    if not isinstance(state, PerformanceState):
        raise TypeError("state must be a PerformanceState value.")
    if state.twr_quality_status is TWRQualityStatus.DATA_INVALID:
        return state
    classification = classify_cash_flow(flow_type)
    if classification.classification_status is not CashFlowClassificationStatus.CONFIRMED:
        return _mark_data_invalid(state)
    if not _finite_amount(cash_flow) or occurred_at is None:
        return _mark_data_invalid(state)

    try:
        event_time = _utc_datetime(occurred_at)
    except ValueError:
        return _mark_data_invalid(state)
    if event_time < _utc_datetime(state.as_of):
        return _mark_data_invalid(state)
    if state.unit_nav <= 0:
        return _mark_data_invalid(state)

    amount = float(cash_flow)
    if classification.flow_type is CashFlowType.DEPOSIT and amount <= 0:
        return _mark_data_invalid(state)
    if classification.flow_type is CashFlowType.WITHDRAWAL and amount >= 0:
        return _mark_data_invalid(state)
    if classification.flow_type is CashFlowType.DISTRIBUTION and amount < 0:
        return _mark_data_invalid(state)

    if classification.adjusts_units:
        new_units = state.units + amount / state.unit_nav
        if not math.isfinite(new_units) or new_units <= 0:
            return _mark_data_invalid(state)
    else:
        new_units = state.units

    return PerformanceState(
        unit_nav=state.unit_nav,
        units=new_units,
        hwm=state.hwm,
        twr_cumulative=state.twr_cumulative,
        drawdown=state.drawdown,
        as_of=event_time,
        twr_quality_status=TWRQualityStatus.VALID,
    )


def apply_external_cash_flow(
    state: PerformanceState,
    *,
    cash_flow: float | None,
    flow_type: str | CashFlowType,
    occurred_at: datetime | None,
) -> PerformanceState:
    """기존 호출 이름을 유지하며 분류된 현금흐름 처리를 위임합니다."""
    return apply_cash_flow(
        state,
        cash_flow=cash_flow,
        flow_type=flow_type,
        occurred_at=occurred_at,
    )


def update_flow_adjusted_performance(
    state: PerformanceState,
    *,
    total_equity: float,
    as_of: datetime,
) -> PerformanceState:
    """평가 시점의 총자산으로 단위가격·TWR·HWM·낙폭을 갱신합니다."""
    if not isinstance(state, PerformanceState):
        raise TypeError("state must be a PerformanceState value.")
    if state.twr_quality_status is TWRQualityStatus.DATA_INVALID:
        raise ValueError("Invalid cash-flow data blocks performance updates.")
    if (
        isinstance(total_equity, bool)
        or not isinstance(total_equity, (int, float))
        or not math.isfinite(total_equity)
        or total_equity < 0
    ):
        raise ValueError("Total equity must be a finite non-negative amount.")
    timestamp = _utc_datetime(as_of)
    if timestamp < _utc_datetime(state.as_of):
        raise ValueError("Performance valuation cannot precede the latest event.")
    if (
        isinstance(state.unit_nav, bool)
        or isinstance(state.units, bool)
        or not isinstance(state.unit_nav, (int, float))
        or not isinstance(state.units, (int, float))
        or not math.isfinite(state.unit_nav)
        or not math.isfinite(state.units)
        or state.unit_nav <= 0
        or state.units <= 0
        or not math.isfinite(state.hwm)
        or state.hwm <= 0
    ):
        raise ValueError("Performance state is invalid.")

    unit_nav = total_equity / state.units
    hwm = max(state.hwm, unit_nav)
    twr_cumulative = unit_nav / 100.0 - 1.0
    drawdown = unit_nav / hwm - 1.0
    if not all(math.isfinite(value) for value in (unit_nav, hwm, twr_cumulative, drawdown)):
        raise ValueError("Performance calculation returned a non-finite value.")
    return PerformanceState(
        unit_nav=unit_nav,
        units=state.units,
        hwm=hwm,
        twr_cumulative=twr_cumulative,
        drawdown=drawdown,
        as_of=timestamp,
        twr_quality_status=TWRQualityStatus.VALID,
    )

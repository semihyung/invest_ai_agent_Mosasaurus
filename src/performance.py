"""현금흐름을 단위화해 TWR과 유량 보정 HWM을 계산합니다."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math

from src.risk_manager import calculate_drawdown


class CashFlowType(str, Enum):
    """계좌 현금 변동을 성과 반영 방식에 따라 분류합니다."""

    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    DISTRIBUTION = "DISTRIBUTION"
    ADJUSTMENT = "ADJUSTMENT"  # 수수료 반올림·이용료 등 성과에 포함하는 내부 조정(단위 조정 없음)
    UNCLASSIFIED = "UNCLASSIFIED"


class CashFlowClassificationStatus(str, Enum):
    """브로커 현금흐름 원인이 확인되었는지 나타냅니다."""

    PROVISIONAL = "PROVISIONAL"  # 1회 관측된 불일치(매수만 대기)
    CONFIRMED = "CONFIRMED"
    UNCLASSIFIED_CASH_FLOW = "UNCLASSIFIED_CASH_FLOW"
    DISMISSED = "DISMISSED"  # 오탐으로 무효화


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
    # v3.7.4: 마감 스냅샷 단위가격, 성과 기간(epoch), 동결·리베이스 표식.
    last_close_unit_nav: float | None = None
    epoch: int = 1
    epoch_closed: bool = False
    hwm_frozen: bool = False
    hwm_rebased: bool = False

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
            or self.units < 0
            or (self.units == 0 and not self.epoch_closed)
            or self.hwm <= 0
            or not -1 <= self.drawdown <= 0
            or isinstance(self.epoch, bool)
            or not isinstance(self.epoch, int)
            or self.epoch < 1
        ):
            raise ValueError("Performance state values are outside valid ranges.")
        if not isinstance(self.twr_quality_status, TWRQualityStatus):
            raise ValueError("twr_quality_status is invalid.")
        object.__setattr__(self, "as_of", _utc_datetime(self.as_of))


MIN_UNITS = 1e-6  # accounting.min_units: 이 값 이하의 보유 단위 수는 0 으로 봅니다.


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
    if state.unit_nav <= 0 or state.epoch_closed:
        return _mark_data_invalid(state)

    amount = float(cash_flow)
    if classification.flow_type is CashFlowType.DEPOSIT and amount <= 0:
        return _mark_data_invalid(state)
    if classification.flow_type is CashFlowType.WITHDRAWAL and amount >= 0:
        return _mark_data_invalid(state)
    if classification.flow_type is CashFlowType.DISTRIBUTION and amount < 0:
        return _mark_data_invalid(state)

    epoch_closed = False
    if classification.adjusts_units:
        # 명세 6.2절: 입출금은 직전 마감 스냅샷의 단위가격으로 단위 수만 조정합니다.
        # (분류가 늦어진 흐름은 `restate_after_late_flow` 로 발생일 직전 단위가격을 써서 따로 반영합니다.)
        reference_nav = state.last_close_unit_nav or state.unit_nav
        new_units = state.units + amount / reference_nav
        if not math.isfinite(new_units) or new_units < -MIN_UNITS:
            return _mark_data_invalid(state)
        if new_units <= MIN_UNITS:
            # 전액 출금: 0으로 나누기를 피하려고 단위가격 계산을 멈추고 성과 기간을 닫습니다.
            new_units, epoch_closed = 0.0, True
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
        last_close_unit_nav=state.last_close_unit_nav,
        epoch=state.epoch,
        epoch_closed=epoch_closed,
        hwm_frozen=state.hwm_frozen,
    )


def update_flow_adjusted_performance(
    state: PerformanceState,
    *,
    total_equity: float,
    as_of: datetime,
    close_snapshot: bool = True,
    freeze: bool = False,
    unclassified_excluded_amount: float = 0.0,
) -> PerformanceState:
    """평가 시점의 총자산으로 단위가격·TWR·낙폭을 갱신하고 HWM은 조건부로만 올립니다.

    HWM은 마감 스냅샷(`close_snapshot=True`)이면서 동결(`freeze`: 미분류 현금흐름 등)이
    아닐 때만 갱신합니다. 그렇지 않으면 HWM을 그대로 두고 낙폭만 계산해, 입금·출금
    직후의 일시적 평가액 변화가 HWM을 오염시키는 것을 막습니다. 전액 출금으로 닫힌
    성과 기간은 새 입금(평가액 > 0)이 확인되면 직전 단위가격에서 이어서 다시 엽니다.

    v3.7.5 오염 제거 단위가격 $U^*$: 미분류 현금흐름이 있으면 그 금액
    (`unclassified_excluded_amount`, 입금 양수·출금 음수)을 평가액에서 뺀 값으로 단위가격을
    계산합니다. 미분류 입금이 수익처럼, 미분류 출금이 손실처럼 보이는 것을 막고, 낙폭 판정은
    동결하지 않고 이 값으로 계속합니다.
    """
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
    if not isinstance(close_snapshot, bool) or not isinstance(freeze, bool):
        raise ValueError("close_snapshot and freeze must be booleans.")
    if not _finite_amount(unclassified_excluded_amount):
        raise ValueError("unclassified_excluded_amount must be a finite amount.")
    # 오염 제거: 미분류 금액을 뺀 평가액을 단위가격 계산에 쓴다(0 미만이면 0 으로 본다).
    total_equity = max(0.0, total_equity - unclassified_excluded_amount)
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
        or not math.isfinite(state.hwm)
        or state.hwm <= 0
    ):
        raise ValueError("Performance state is invalid.")

    units, epoch, epoch_closed = state.units, state.epoch, state.epoch_closed
    if epoch_closed:
        if total_equity <= 0:
            # 아직 입금이 없으므로 단위가격을 계산하지 않고(0으로 나누기 방지) 상태를 유지합니다.
            return PerformanceState(
                unit_nav=state.unit_nav, units=0.0, hwm=state.hwm,
                twr_cumulative=state.twr_cumulative, drawdown=state.drawdown,
                as_of=timestamp, last_close_unit_nav=state.last_close_unit_nav,
                epoch=epoch, epoch_closed=True, hwm_frozen=freeze,
            )
        # 새 성과 기간: 직전 유효 단위가격에서 이어 시작하고 HWM도 이어받습니다.
        units = total_equity / state.unit_nav
        epoch, epoch_closed = epoch + 1, False
    elif units <= MIN_UNITS:
        raise ValueError("Performance state is invalid.")

    unit_nav = total_equity / units
    update_hwm = close_snapshot and not freeze
    hwm = max(state.hwm, unit_nav) if update_hwm else state.hwm
    twr_cumulative = unit_nav / 100.0 - 1.0
    drawdown = min(0.0, calculate_drawdown(unit_nav, hwm))
    if not all(math.isfinite(value) for value in (unit_nav, hwm, twr_cumulative, drawdown)):
        raise ValueError("Performance calculation returned a non-finite value.")
    return PerformanceState(
        unit_nav=unit_nav,
        units=units,
        hwm=hwm,
        twr_cumulative=twr_cumulative,
        drawdown=drawdown,
        as_of=timestamp,
        twr_quality_status=TWRQualityStatus.VALID,
        last_close_unit_nav=unit_nav if close_snapshot else state.last_close_unit_nav,
        epoch=epoch,
        epoch_closed=epoch_closed,
        hwm_frozen=freeze,
    )


@dataclass(frozen=True)
class SnapshotRow:
    """재계산 대상 일일 스냅샷의 최소 입력(`daily_portfolio` 한 행)입니다."""

    snapshot_date: str
    total_equity: float
    excluded_amount: float  # 이 스냅샷 계산에서 제외했던 미분류 금액(입금 +, 출금 −)
    units: float
    hwm: float  # 그 날 마감 시점의 HWM


@dataclass(frozen=True)
class RestatedRow:
    """늦은 현금흐름 확정으로 다시 계산된 스냅샷입니다."""

    snapshot_date: str
    units: float
    excluded_amount: float
    unit_nav: float
    hwm: float
    restated: bool


def restate_after_late_flow(
    snapshots: list[SnapshotRow],
    *,
    flow_date: str,
    amount: float,
    flow_type: str | CashFlowType,
    hwm_before_flow: float,
) -> tuple[float | None, float | None, list[RestatedRow]]:
    """늦게 분류된 입출금을 발생일 직전 단위가격으로 반영해 스냅샷을 다시 계산합니다(명세 6.2절 v3.7.5).

    반환값은 (적용한 단위가격, 단위 수 변화 ΔN, 재계산된 행들)입니다.
    - `DEPOSIT`·`WITHDRAWAL`: 단위 수 변화 ΔN = F / U*(발생일 직전)을 발생일부터 모든 스냅샷의 단위 수에
      더하고, 제외했던 금액에서 F 를 빼서(더는 미분류가 아님) 단위가격을 다시 구한 뒤 HWM 도 다시 산출한다.
    - `DISTRIBUTION`·`ADJUSTMENT`: 단위 조정 없이 제외 금액만 빼서 성과에 포함한다(ΔN = 0).
    `snapshots` 는 날짜 오름차순이며 발생일 이후 행만 재계산한다. 발생일 직전 행이 없으면
    기준 단위가격을 알 수 없어 (None, None, []) 를 반환한다(호출자가 수동 확인).
    """
    classified = CashFlowType(flow_type)
    if classified in (CashFlowType.UNCLASSIFIED,):
        raise ValueError("Only a classified cash flow can be restated.")
    ordered = sorted(snapshots, key=lambda row: row.snapshot_date)
    before = [row for row in ordered if row.snapshot_date < flow_date]
    after = [row for row in ordered if row.snapshot_date >= flow_date]
    if not after:
        return None, 0.0, []
    delta_units = 0.0
    reference_nav: float | None = None
    if classified in (CashFlowType.DEPOSIT, CashFlowType.WITHDRAWAL):
        if not before:
            return None, None, []
        previous = before[-1]
        reference_nav = (previous.total_equity - previous.excluded_amount) / previous.units
        if not math.isfinite(reference_nav) or reference_nav <= 0:
            return None, None, []
        delta_units = amount / reference_nav
    rows: list[RestatedRow] = []
    running_hwm = hwm_before_flow
    for row in after:
        units = row.units + delta_units
        excluded = row.excluded_amount - amount
        if units <= MIN_UNITS:
            raise ValueError("Restated unit count is not positive.")
        unit_nav = (row.total_equity - excluded) / units
        running_hwm = max(running_hwm, unit_nav)
        rows.append(RestatedRow(row.snapshot_date, units, excluded, unit_nav, running_hwm, True))
    return reference_nav, delta_units, rows


def rebase_hwm(state: PerformanceState) -> PerformanceState:
    """EMERGENCY 해제 시 HWM만 현재 단위가격으로 리베이스합니다(TWR 누적은 불변)."""
    if not isinstance(state, PerformanceState):
        raise TypeError("state must be a PerformanceState value.")
    if state.epoch_closed or state.unit_nav <= 0:
        raise ValueError("Cannot rebase the HWM without a valid unit price.")
    return PerformanceState(
        unit_nav=state.unit_nav, units=state.units, hwm=state.unit_nav,
        twr_cumulative=state.twr_cumulative, drawdown=0.0, as_of=state.as_of,
        twr_quality_status=state.twr_quality_status,
        last_close_unit_nav=state.last_close_unit_nav, epoch=state.epoch,
        hwm_frozen=state.hwm_frozen, hwm_rebased=True,
    )

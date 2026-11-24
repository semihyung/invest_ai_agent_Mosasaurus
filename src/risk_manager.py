"""수정주가 손절과 운영 모드별 주문 권한을 중앙에서 판정합니다."""

from dataclasses import dataclass
from enum import Enum
import math


class DrawdownMode(str, Enum):
    """계좌 단위가격 낙폭에 따른 위험 단계를 나타냅니다."""

    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class OperatingMode(str, Enum):
    """독립 운영 플래그를 우선순위로 합친 유효 운영 모드입니다."""

    SAFE_HALT = "SAFE_HALT"
    EMERGENCY = "EMERGENCY"
    PAUSED = "PAUSED"
    TRAINING = "TRAINING"
    NORMAL = "NORMAL"


class RiskAction(str, Enum):
    """위험 정책이 허용 여부를 판단하는 주문·조회 행위입니다."""

    BUY_RISK_ASSET = "BUY_RISK_ASSET"
    SMART_DCA_BUY = "SMART_DCA_BUY"
    BUY_PARKING = "BUY_PARKING"
    REDUCE_ONLY_SELL = "REDUCE_ONLY_SELL"
    QUERY = "QUERY"
    RECONCILE = "RECONCILE"
    CANCEL = "CANCEL"


@dataclass(frozen=True)
class RiskFlags:
    """DB에서 읽은 운영 플래그와 계좌 낙폭 단계를 보관합니다."""

    is_safe_halt: bool = False
    emergency_mode: bool = False
    is_paused: bool = False
    training_mode: bool = False
    data_invalid: bool = False
    drawdown_mode: DrawdownMode = DrawdownMode.NORMAL

    def __post_init__(self) -> None:
        """플래그의 자료형이 모호한 참/거짓 값으로 오염되지 않았는지 봅니다."""
        for value in (
            self.is_safe_halt,
            self.emergency_mode,
            self.is_paused,
            self.training_mode,
            self.data_invalid,
        ):
            if not isinstance(value, bool):
                raise ValueError("Risk mode flags must be booleans.")
        if not isinstance(self.drawdown_mode, DrawdownMode):
            raise ValueError("drawdown_mode must be a DrawdownMode value.")


def trailing_stop_ratio(
    training_mode: bool = False,
    *,
    normal_threshold: float = -0.10,
    training_threshold: float = -0.07,
) -> float:
    """일반 -10% 또는 훈련 -7%의 수정주가 손절 기준을 반환합니다."""
    if not isinstance(training_mode, bool):
        raise ValueError("training_mode must be a boolean.")
    for threshold in (normal_threshold, training_threshold):
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or not -1 < threshold < 0
        ):
            raise ValueError("Trailing stop thresholds must be finite negative ratios.")
    return training_threshold if training_mode else normal_threshold


def should_trailing_sell(
    current_price: float,
    peak_price: float,
    *,
    training_mode: bool = False,
    normal_threshold: float = -0.10,
    training_threshold: float = -0.07,
) -> bool:
    """현재 수정 기준 가격이 수정 기준 최고가에서 손절 폭에 도달했는지 봅니다."""
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        for value in (current_price, peak_price)
    ):
        raise ValueError("Adjusted prices must be finite positive numbers.")
    threshold = trailing_stop_ratio(
        training_mode,
        normal_threshold=normal_threshold,
        training_threshold=training_threshold,
    )
    return current_price <= peak_price * (1 + threshold)


def update_adjusted_peak_price(
    previous_peak: float,
    current_adjusted_price: float,
) -> float:
    """유효한 수정주가 최고가만 단조 증가 방향으로 갱신합니다."""
    for price in (previous_peak, current_adjusted_price):
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
        ):
            raise ValueError("Adjusted prices must be finite positive numbers.")
    return max(previous_peak, current_adjusted_price)


def calculate_drawdown(current_equity: float, hwm_equity: float) -> float:
    """단위화된 현재 평가액과 유량 보정 HWM으로 계좌 낙폭을 계산합니다."""
    if (
        isinstance(current_equity, bool)
        or isinstance(hwm_equity, bool)
        or not isinstance(current_equity, (int, float))
        or not isinstance(hwm_equity, (int, float))
        or not math.isfinite(current_equity)
        or not math.isfinite(hwm_equity)
        or current_equity < 0
        or hwm_equity <= 0
    ):
        raise ValueError("Equity values must be finite and valid.")
    return current_equity / hwm_equity - 1


def get_drawdown_mode(
    drawdown: float,
    *,
    caution_threshold: float = -0.07,
    warning_threshold: float = -0.10,
    critical_threshold: float = -0.15,
) -> DrawdownMode:
    """계좌 낙폭을 CAUTION/WARNING/CRITICAL의 포함형 기준으로 매핑합니다."""
    if (
        isinstance(drawdown, bool)
        or not isinstance(drawdown, (int, float))
        or not math.isfinite(drawdown)
        or drawdown < -1
        or drawdown > 0
    ):
        raise ValueError("Drawdown must be a finite ratio between -1 and 0.")
    thresholds = (caution_threshold, warning_threshold, critical_threshold)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in thresholds
    ) or not 0 > caution_threshold > warning_threshold > critical_threshold >= -1:
        raise ValueError("Drawdown thresholds must be ordered negative ratios.")
    if drawdown <= critical_threshold:
        return DrawdownMode.CRITICAL
    if drawdown <= warning_threshold:
        return DrawdownMode.WARNING
    if drawdown <= caution_threshold:
        return DrawdownMode.CAUTION
    return DrawdownMode.NORMAL


_STAGE_ORDER = (
    DrawdownMode.NORMAL,
    DrawdownMode.CAUTION,
    DrawdownMode.WARNING,
    DrawdownMode.CRITICAL,
)
# 각 단계에서 벗어나려면 넘어야 하는 경계(이 값 + recovery_buffer 보다 낙폭이 얕아져야 함).
_STAGE_BOUNDARY = {
    DrawdownMode.CAUTION: -0.07,
    DrawdownMode.WARNING: -0.10,
    DrawdownMode.CRITICAL: -0.15,
}


def next_drawdown_stage(
    previous: DrawdownMode,
    drawdown: float,
    *,
    recovery_buffer: float = 0.02,
    frozen: bool = False,
    floor_stage: DrawdownMode | None = None,
) -> DrawdownMode:
    """히스테리시스가 적용된 다음 낙폭 단계를 반환합니다(명세 6.2절).

    - 악화 방향 전이는 즉시 일어나지만, `frozen`(미분류 현금흐름 등)이면 동결합니다.
    - 회복 방향 전이는 낙폭이 경계보다 `recovery_buffer` 이상 회복됐을 때만 한 단계씩 풉니다.
    - `floor_stage` 는 `EMERGENCY` 해제 직후 일정 기간 유지할 최소 단계입니다.
    """
    if not isinstance(previous, DrawdownMode):
        raise TypeError("previous must be a DrawdownMode value.")
    if (
        isinstance(recovery_buffer, bool)
        or not isinstance(recovery_buffer, (int, float))
        or not math.isfinite(recovery_buffer)
        or recovery_buffer < 0
    ):
        raise ValueError("recovery_buffer must be a finite non-negative ratio.")
    raw = get_drawdown_mode(drawdown)
    rank = {stage: index for index, stage in enumerate(_STAGE_ORDER)}
    stage = previous
    if rank[raw] > rank[previous]:
        stage = previous if frozen else raw
    else:
        while rank[stage] > rank[raw] and drawdown > _STAGE_BOUNDARY[stage] + recovery_buffer:
            stage = _STAGE_ORDER[rank[stage] - 1]
    if floor_stage is not None and rank[stage] < rank[floor_stage]:
        stage = floor_stage
    return stage


def resolve_operating_mode(flags: RiskFlags) -> OperatingMode:
    """SAFE_HALT, EMERGENCY, PAUSED, TRAINING, NORMAL 순으로 유효 모드를 고릅니다."""
    if not isinstance(flags, RiskFlags):
        raise TypeError("flags must be a RiskFlags value.")
    if flags.is_safe_halt or flags.data_invalid:
        return OperatingMode.SAFE_HALT
    if flags.emergency_mode or flags.drawdown_mode is DrawdownMode.CRITICAL:
        return OperatingMode.EMERGENCY
    if flags.is_paused:
        return OperatingMode.PAUSED
    if flags.training_mode:
        return OperatingMode.TRAINING
    return OperatingMode.NORMAL


def is_action_allowed(
    flags: RiskFlags,
    action: RiskAction,
    *,
    hard_stop_triggered: bool = False,
    safety_gates_passed: bool = False,
) -> bool:
    """단일 정책으로 운영 모드·낙폭·축소매도 안전 게이트를 판정합니다."""
    if not isinstance(flags, RiskFlags):
        raise TypeError("flags must be a RiskFlags value.")
    if not isinstance(action, RiskAction):
        raise ValueError("action must be a RiskAction value.")
    if not isinstance(hard_stop_triggered, bool) or not isinstance(
        safety_gates_passed,
        bool,
    ):
        raise ValueError("Risk action gate inputs must be booleans.")
    mode = resolve_operating_mode(flags)

    if action in (RiskAction.QUERY, RiskAction.RECONCILE, RiskAction.CANCEL):
        return True
    if mode is OperatingMode.SAFE_HALT:
        return False
    if action is RiskAction.BUY_RISK_ASSET:
        return (
            mode is OperatingMode.NORMAL
            and flags.drawdown_mode
            in (DrawdownMode.NORMAL, DrawdownMode.CAUTION)
        )
    if action is RiskAction.SMART_DCA_BUY:
        return (
            mode is OperatingMode.NORMAL
            and flags.drawdown_mode is DrawdownMode.NORMAL
        )
    if action is RiskAction.BUY_PARKING:
        return mode in (
            OperatingMode.NORMAL,
            OperatingMode.TRAINING,
        ) and flags.drawdown_mode is not DrawdownMode.CRITICAL
    if action is RiskAction.REDUCE_ONLY_SELL:
        return hard_stop_triggered and safety_gates_passed
    return False


def allows_risk_asset_buy(flags: RiskFlags) -> bool:
    """위험자산 매수 권한을 공통 정책 판정기에 위임합니다."""
    return is_action_allowed(flags, RiskAction.BUY_RISK_ASSET)


def allows_parking_buy(flags: RiskFlags) -> bool:
    """파킹 자산 매수 권한을 공통 정책 판정기에 위임합니다."""
    return is_action_allowed(flags, RiskAction.BUY_PARKING)


def allows_smart_dca_buy(flags: RiskFlags) -> bool:
    """낙폭 CAUTION 이상에서 중단해야 하는 Smart DCA 매수를 판정합니다."""
    return is_action_allowed(flags, RiskAction.SMART_DCA_BUY)


def allows_reduce_only_sell(
    flags: RiskFlags,
    *,
    hard_stop_triggered: bool = False,
    safety_gates_passed: bool = False,
) -> bool:
    """유효한 하드 스탑과 주문 안전 게이트가 모두 통과한 축소 매도만 허용합니다."""
    return is_action_allowed(
        flags,
        RiskAction.REDUCE_ONLY_SELL,
        hard_stop_triggered=hard_stop_triggered,
        safety_gates_passed=safety_gates_passed,
    )


def allows_new_order(flags: RiskFlags) -> bool:
    """기존 호출부 호환을 위해 위험자산 신규 매수 가능 여부를 반환합니다."""
    return allows_risk_asset_buy(flags)

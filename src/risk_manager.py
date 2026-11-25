"""수정주가 손절과 운영 모드별 주문 권한을 중앙에서 판정합니다."""

from dataclasses import dataclass
from enum import Enum
import math
from typing import Collection

# 명세 6.3절(v3.7.5 D1): `SAFE_HALT` 중 보호성 손절이 가능한 원인과 스탑 비율은 코드 상수입니다.
# `rules.yaml` 로 넓힐 수 없고(10.5절 검사), `CONFIG_INVALID` 중에도 설정값에 의존하지 않기 위해서입니다.
PROTECTIVE_EXIT_REASONS = frozenset(
    {"DEPLOY_ROLLBACK", "ACCOUNT_MISMATCH", "UPS_LOW", "CONFIG_INVALID"}
)
PROTECTIVE_STOP_NORMAL = -0.10
PROTECTIVE_STOP_TRAINING = -0.07


def protective_exit_permitted(
    halt_reasons: Collection[str],
    *,
    enabled: bool = True,
) -> bool:
    """활성 `SAFE_HALT` 원인이 1개 이상이고 전부 보호성 손절 허용 원인일 때만 True 입니다.

    `PENDING_CONFIRMATION`·`CREDENTIALS_UNAVAILABLE`·`DB_INTEGRITY`·`BOOT_RECOVERY_FAILED`·
    `CALENDAR_MISMATCH`·`CLOCK_UNSYNCED` 가 하나라도 섞여 있으면 False(전면 차단 유지)입니다.
    """
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean.")
    reasons = frozenset(halt_reasons)
    return enabled and bool(reasons) and reasons <= PROTECTIVE_EXIT_REASONS


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


@dataclass(frozen=True)
class RiskFlags:
    """DB에서 읽은 운영 플래그와 계좌 낙폭 단계를 보관합니다."""

    is_safe_halt: bool = False
    emergency_mode: bool = False
    is_paused: bool = False
    training_mode: bool = False
    data_invalid: bool = False
    drawdown_mode: DrawdownMode = DrawdownMode.NORMAL
    # 활성 SAFE_HALT 원인 코드와 보호성 손절 사용 여부(`risk.safe_halt.protective_exit.enabled`).
    halt_reasons: frozenset = frozenset()
    protective_exit_enabled: bool = True

    def __post_init__(self) -> None:
        """플래그의 자료형이 모호한 참/거짓 값으로 오염되지 않았는지 봅니다."""
        if not isinstance(self.halt_reasons, frozenset):
            raise ValueError("halt_reasons must be a frozenset of reason codes.")
        for value in (
            self.is_safe_halt,
            self.emergency_mode,
            self.is_paused,
            self.training_mode,
            self.data_invalid,
            self.protective_exit_enabled,
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


def should_protective_stop(
    current_price: float,
    peak_price: float,
    *,
    training_mode: bool = False,
) -> bool:
    """보호성 손절 판정: 코드 상수(-10%, 훈련 -7%)만 쓰는 트레일링 스탑입니다(120MA 등은 해당 없음)."""
    return should_trailing_sell(
        current_price,
        peak_price,
        training_mode=training_mode,
        normal_threshold=PROTECTIVE_STOP_NORMAL,
        training_threshold=PROTECTIVE_STOP_TRAINING,
    )


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

    - 악화 방향 전이는 항상 즉시 일어납니다(v3.7.5: 미분류 현금흐름 중에도 오염 제거 단위가격
      기준으로 계속 판정해 `/classify` 대기 중 무방비 구간이 생기지 않게 합니다).
    - `frozen`(미분류 현금흐름 등)이면 회복 방향 전이만 동결합니다(분류 확정 후 재계산된 스냅샷에서 풉니다).
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
        stage = raw
    elif not frozen:
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

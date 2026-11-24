"""명세 0.6절 진리표: (운영 모드, 낙폭 단계, 주문 종류) 하나로 주문 허용 여부를 판정합니다.

모든 주문 경로는 이 함수 하나만 통과해야 합니다. 이 모듈은 DB·네트워크에 접근하지
않는 순수 함수라서, 표의 모든 조합을 단위 테스트로 빠짐없이 검증할 수 있습니다.
"""

from enum import Enum

from typing import Collection

from src.risk_manager import DrawdownMode, OperatingMode, protective_exit_permitted


class OrderKind(str, Enum):
    """진리표의 열에 해당하는 주문·요청 종류입니다."""

    RISK_BUY = "RISK_BUY"                      # 위험자산 전략 매수(신규 슬롯 진입)
    RISK_TOP_UP_BUY = "RISK_TOP_UP_BUY"        # 기존 보유 종목 Drift 보충 매수
    RISK_ROTATION_SELL = "RISK_ROTATION_SELL"  # 위험자산 정기 교체·리밸런싱 매도
    PARKING_BUY = "PARKING_BUY"                # 대표 SAFE_PARKING 매수
    PARKING_SELL = "PARKING_SELL"              # 파킹 현금화 매도
    REDUCE_ONLY_SELL = "REDUCE_ONLY_SELL"      # Risk Manager 가 만든 위험 축소 매도
    PROTECTIVE_EXIT_SELL = "PROTECTIVE_EXIT_SELL"  # SAFE_HALT 중 트레일링 스탑 한정 매도(6.3절 D1)
    EXIT_OBLIGATION_RETRY = "EXIT_OBLIGATION_RETRY"  # 익일 검문소 매도 의무 재주문
    CANCEL = "CANCEL"                          # 취소 요청(항상 허용)
    SMOKE_TEST = "SMOKE_TEST"                  # 1주 스모크 테스트 전용


# 모드별로 허용되는 종류(낙폭 단계 제한은 아래 함수에서 추가 적용).
_ALLOWED_BY_MODE: dict[OperatingMode, frozenset[OrderKind]] = {
    OperatingMode.SAFE_HALT: frozenset({OrderKind.CANCEL}),
    OperatingMode.EMERGENCY: frozenset(
        {
            OrderKind.REDUCE_ONLY_SELL,
            OrderKind.PROTECTIVE_EXIT_SELL,
            OrderKind.EXIT_OBLIGATION_RETRY,
            OrderKind.CANCEL,
        }
    ),
    OperatingMode.PAUSED: frozenset(
        {
            OrderKind.REDUCE_ONLY_SELL,
            OrderKind.PROTECTIVE_EXIT_SELL,
            OrderKind.EXIT_OBLIGATION_RETRY,
            OrderKind.CANCEL,
        }
    ),
    OperatingMode.TRAINING: frozenset(
        {
            OrderKind.PARKING_BUY,
            OrderKind.REDUCE_ONLY_SELL,
            OrderKind.PROTECTIVE_EXIT_SELL,
            OrderKind.EXIT_OBLIGATION_RETRY,
            OrderKind.CANCEL,
        }
    ),
    OperatingMode.NORMAL: frozenset(OrderKind),
}


def is_allowed(
    mode: OperatingMode,
    stage: DrawdownMode,
    kind: OrderKind,
    *,
    halt_reasons: Collection[str] = frozenset(),
    protective_exit_enabled: bool = True,
) -> bool:
    """진리표(0.6절)에 따라 해당 주문 종류의 허용 여부를 반환합니다.

    - 취소는 어떤 모드에서도 허용합니다(조회·대조도 주문이 아니므로 별도 제한 없음).
    - `SAFE_HALT` 에서는 취소 외 모든 주문(매수·매도)을 차단합니다. 유일한 예외는 활성 원인이
      모두 보호성 손절 허용 원인(`halt_reasons`)일 때의 `PROTECTIVE_EXIT_SELL` 입니다(v3.7.5 D1).
    - 낙폭 단계: CAUTION 이면 Drift 보충 매수 금지, WARNING 이면 위험자산 매수 전면 금지,
      스모크 테스트는 단계가 NORMAL 일 때만 허용합니다. CRITICAL 단계는 운영 모드가
      EMERGENCY 로 자동 전환되므로 NORMAL/TRAINING 과 함께 올 수 없습니다.
    """
    if not isinstance(mode, OperatingMode):
        raise TypeError("mode must be an OperatingMode value.")
    if not isinstance(stage, DrawdownMode):
        raise TypeError("stage must be a DrawdownMode value.")
    if not isinstance(kind, OrderKind):
        raise TypeError("kind must be an OrderKind value.")

    if (
        mode is OperatingMode.SAFE_HALT
        and kind is OrderKind.PROTECTIVE_EXIT_SELL
        and protective_exit_permitted(halt_reasons, enabled=protective_exit_enabled)
    ):
        return True
    if kind not in _ALLOWED_BY_MODE[mode]:
        return False
    if kind is OrderKind.CANCEL:
        return True
    # CRITICAL 인데 모드가 EMERGENCY 가 아니면 상태가 모순이므로 fail-closed 합니다.
    if stage is DrawdownMode.CRITICAL and mode is not OperatingMode.EMERGENCY:
        return kind in (
            OrderKind.REDUCE_ONLY_SELL,
            OrderKind.PROTECTIVE_EXIT_SELL,
            OrderKind.EXIT_OBLIGATION_RETRY,
        )

    if kind is OrderKind.RISK_BUY:
        return stage in (DrawdownMode.NORMAL, DrawdownMode.CAUTION)
    if kind is OrderKind.RISK_TOP_UP_BUY:
        return stage is DrawdownMode.NORMAL
    if kind is OrderKind.SMOKE_TEST:
        return stage is DrawdownMode.NORMAL
    return True

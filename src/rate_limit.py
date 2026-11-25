"""프로세스 단일 우선순위 호출 한도기(명세 4.4절 "호출 우선순위").

KIS 호출은 모두 한 토큰 버킷을 지나며, 우선순위는 **주문·취소·대조(0) > 스탑 감시(1) > 일봉·기타 조회(2)** 입니다.
높은 우선순위 호출이 대기 중이면 낮은 우선순위는 순서를 양보합니다(굶주림을 막기 위해 한도기는 호출당 최소 간격만 강제).
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

KST = timezone(timedelta(hours=9), name="KST")
ORDER, GUARD, OTHER = 0, 1, 2


class PriorityRateLimiter:
    """초당 `rate` 회를 넘지 않게 하되, 대기자가 여럿이면 우선순위가 높은 쪽부터 통과시킨다."""

    def __init__(self, rate_per_second: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        """초당 호출 한도를 설정한다."""
        if isinstance(rate_per_second, bool) or not isinstance(rate_per_second, (int, float)) or rate_per_second <= 0:
            raise ValueError("rate_per_second must be a positive number.")
        self._interval = 1.0 / rate_per_second
        self._clock = clock
        self._next_allowed = 0.0
        self._waiting = {ORDER: 0, GUARD: 0, OTHER: 0}
        self._cond = threading.Condition()
        self._calls = {}  # KST 날짜 → 그날 통과한 호출 수(일일 브리핑의 API 쿼터 표시용)

    def acquire(self, priority: int) -> None:
        """호출 1회분 슬롯을 얻을 때까지 대기한다."""
        if priority not in self._waiting:
            raise ValueError("Unknown priority.")
        with self._cond:
            self._waiting[priority] += 1
            try:
                while True:
                    higher_waiting = any(self._waiting[p] for p in range(priority))
                    wait = self._next_allowed - self._clock()
                    if not higher_waiting and wait <= 0:
                        break
                    self._cond.wait(timeout=0.05 if higher_waiting else min(max(wait, 0.001), 0.05))
            finally:
                self._waiting[priority] -= 1
            self._next_allowed = max(self._clock(), self._next_allowed) + self._interval
            day = datetime.now(KST).date()
            self._calls = {day: self._calls.get(day, 0) + 1}
            self._cond.notify_all()


    def calls_today(self) -> int:
        """오늘(KST) 한도기를 통과한 KIS 호출 수."""
        return self._calls.get(datetime.now(KST).date(), 0)


class _LimitedTransport:
    """주문 전송기 호출(전송·취소)을 최우선(ORDER)으로 한도기에 통과시킨다."""

    def __init__(self, inner: Any, limiter: PriorityRateLimiter) -> None:
        """래핑할 객체와 한도기를 보관한다."""
        self._inner, self._limiter = inner, limiter

    def __getattr__(self, name: str) -> Any:
        """주문 전송기의 호출 메서드를 최우선 한도기로 감싼다."""
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            """한도 슬롯을 얻은 뒤 원래 메서드를 호출한다."""
            self._limiter.acquire(ORDER)
            return target(*args, **kwargs)

        return call


_PRIORITY = {
    "quote": GUARD, "last_price": GUARD,
    "positions": ORDER, "orders": ORDER, "cash_d2": ORDER, "total_equity": ORDER, "sellable_qty": ORDER,
    "fill_prices": ORDER, "cancel_order": ORDER, "buyable_cash": ORDER,
    "candles": OTHER,
}


class RateLimitedBroker:
    """브로커 포트의 모든 조회·주문 호출을 한도기에 통과시키는 투명한 래퍼."""

    def __init__(self, inner: Any, limiter: PriorityRateLimiter) -> None:
        """래핑할 브로커와 한도기를 보관한다."""
        self._inner, self._limiter = inner, limiter
        self.transport = _LimitedTransport(inner.transport, limiter)
        self.environment = inner.environment

    def __getattr__(self, name: str) -> Any:
        """조회·주문 메서드를 우선순위에 맞춰 한도기로 감싼다."""
        target = getattr(self._inner, name)
        if name not in _PRIORITY or not callable(target):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            """우선순위 슬롯을 얻은 뒤 원래 메서드를 호출한다."""
            self._limiter.acquire(_PRIORITY[name])
            return target(*args, **kwargs)

        return call


def guard_poll_interval(position_count: int, rate_per_second: float, base_seconds: int = 10) -> int:
    """보유 종목당 현재가 1콜 × 종목 수가 한도를 넘으면 `ceil(필요 호출 수 / 초당 한도)` 초로 폴링 주기를 늘린다."""
    import math

    if position_count <= 0:
        return base_seconds
    return max(base_seconds, math.ceil(position_count / rate_per_second))

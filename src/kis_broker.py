"""KIS 조회 클라이언트·주문 전송기를 `job_handlers` 의 `AccountBrokerPort` 로 묶는 얇은 어댑터입니다.

KIS 응답 필드명 중 `[UNVERIFIED]` 표시가 붙은 것은 포털 실측 전까지 값이 없으면 예외를 올립니다
(fail-closed: 호출한 잡이 `ACCOUNT_DATA_STALE` 로 처리하므로 추측 값으로 매매하지 않습니다).
주문 전송기는 외부에서 주입받으며 이 모듈은 비공개 주문 어댑터를 직접 import 하지 않습니다.
"""

from datetime import date, timedelta
from typing import Any, Callable, Mapping

from src.market_data import DailyCandle, daily_candles_from_kis
from src.order_manager import Quote
from src.reconciliation import OrderSnapshot, Position


def _int(row: Mapping[str, Any], key: str) -> int:
    """KIS 숫자 문자열을 정수로 바꾼다. 없거나 형식이 틀리면 ValueError(추측하지 않음)."""
    value = row.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"KIS field missing: {key}")
    return int(float(str(value).replace(",", "")))


class KisBroker:
    """운영(PAPER 우선) 증권사 포트. `environment` 는 주입된 읽기 클라이언트의 환경을 따른다."""

    def __init__(
        self,
        client: Any,
        transport: Any,
        *,
        account_number: str,
        environment: str = "PAPER",
        order_lookup: Callable[[str], tuple[str, int, int] | None] = lambda _no: None,
    ) -> None:
        """order_lookup: kis_order_no → (조직번호, 잔량, 주문가격). 취소 요청 본문에 필요하다."""
        self._client, self.transport = client, transport
        self._account, self.environment, self._lookup = account_number, environment, order_lookup
        self._last_executions: tuple[date, list] | None = None  # orders() 가 받은 행을 fill_prices() 가 1회 재사용

    def _balance(self) -> tuple[list[dict], dict]:
        """잔고 조회 결과(보유 목록, 요약 한 건)를 돌려준다."""
        holdings, summary = self._client.fetch_account_balance(self._account)
        return holdings, (summary[0] if isinstance(summary, list) and summary else summary)

    def quote(self, ticker: str) -> Quote:
        """Bid1/Ask1 (iNAV 는 별도 WebSocket 경로라 여기서는 None → 위험 축소 매도를 막지 않음)."""
        book = self._client.fetch_orderbook(ticker)
        return Quote(bid=_int(book, "bidp1"), ask=_int(book, "askp1"), nav=None)

    def last_price(self, ticker: str) -> float:
        """현재가(`stck_prpr`)."""
        return float(_int(self._client.fetch_etf_quote(ticker), "stck_prpr"))

    def positions(self) -> list[Position]:
        """보유 종목·수량(`pdno`, `hldg_qty`)."""
        holdings, _ = self._balance()
        return [Position(r["pdno"], _int(r, "hldg_qty")) for r in holdings if _int(r, "hldg_qty") > 0]

    def sellable_qty(self, ticker: str) -> int:
        """매도가능수량(`ord_psbl_qty`). 보유가 없으면 0."""
        holdings, _ = self._balance()
        return next((_int(r, "ord_psbl_qty") for r in holdings if r.get("pdno") == ticker), 0)

    def cash_d2(self) -> int:
        """D+2 정산 예수금 `prvs_rcdl_excc_amt` `[UNVERIFIED]` — 이 필드 하나로만 대사한다."""
        return _int(self._balance()[1], "prvs_rcdl_excc_amt")

    def total_equity(self) -> int:
        """총평가금액 `tot_evlu_amt` `[UNVERIFIED]`."""
        return _int(self._balance()[1], "tot_evlu_amt")

    def orders(self, trade_date: date) -> list[OrderSnapshot]:
        """당일 주문·체결 내역을 DB 상태명과 같은 상태로 정규화한다(`odno`, `ord_qty`, `tot_ccld_qty`, `rmn_qty`)."""
        rows, _ = self._client.fetch_daily_order_executions(self._account, trade_date)
        self._last_executions = (trade_date, rows)
        snapshots = []
        for r in rows:
            ordered, filled, remaining = _int(r, "ord_qty"), _int(r, "tot_ccld_qty"), _int(r, "rmn_qty")
            if r.get("cncl_yn") == "Y":
                status = "CANCELED"
            elif remaining == 0 and filled > 0:
                status = "FILLED"
            elif filled > 0:
                status = "PARTIALLY_FILLED"
            else:
                status = "ACCEPTED"
            snapshots.append(OrderSnapshot(
                r["odno"], r["pdno"], "SELL" if r.get("sll_buy_dvsn_cd") == "01" else "BUY",
                ordered, filled, remaining, status))
        return snapshots

    def candles(self, ticker: str, today: date) -> list[DailyCandle]:
        """직전 완료 거래일까지 최근 200거래일의 원주가·수정주가 일봉(오늘 진행 중인 봉은 제외)."""
        start = today - timedelta(days=330)
        raw = self._client.fetch_daily_candles(ticker, start, today, adjusted=False)
        adjusted = self._client.fetch_daily_candles(ticker, start, today, adjusted=True)
        done = [c for c in daily_candles_from_kis(ticker, raw, adjusted) if c.trade_date < today.isoformat()]
        return done[-200:]

    def buyable_cash(self) -> int:
        """매수가능금액 `nrcvb_buy_amt`: 조회 TR 미확정 `[UNVERIFIED]` → 확정 전까지 예외(= 신규 매수 fail-closed)."""
        raise NotImplementedError("buyable-cash TR is not verified; new buys stay blocked (spec 5.4).")

    def fill_prices(self, trade_date: date) -> dict[str, float]:
        """주문번호 → 평균 체결가(`avg_prvs`) `[UNVERIFIED]`. 값이 없는 주문은 빼고 호출자가 주문가로 대체한다.

        바로 앞 `orders()` 가 같은 날짜를 조회했으면 그 행을 한 번만 재사용한다: API 호출이 절반이 되고,
        체결 수량과 평균가가 같은 시점의 응답에서 나온다.
        """
        cached, self._last_executions = self._last_executions, None
        if cached is not None and cached[0] == trade_date:
            rows = cached[1]
        else:
            rows, _ = self._client.fetch_daily_order_executions(self._account, trade_date)
        return {r["odno"]: float(r["avg_prvs"]) for r in rows
                if str(r.get("avg_prvs", "")).strip() not in ("", "0")}

    def cancel_order(self, kis_order_no: str, ticker: str) -> bool:
        """미체결 잔량 전부 취소를 1회 요청한다. 주문 정보를 모르면 요청하지 않는다(False)."""
        info = self._lookup(kis_order_no)
        if info is None:
            return False
        org_no, remaining, price = info
        ack = self.transport.cancel_order(
            org_number=org_no, order_number=kis_order_no, quantity=remaining, price=price)
        return bool(getattr(ack, "accepted", False))

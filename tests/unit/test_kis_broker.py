"""kis_broker: KIS 응답 → 포트 변환과 fail-closed 동작을 가짜 클라이언트로 검증합니다."""

import unittest
from datetime import date

from src.kis_broker import KisBroker


class FakeClient:
    def __init__(self, summary=None):
        self.summary = summary if summary is not None else [{"prvs_rcdl_excc_amt": "1,000", "tot_evlu_amt": "5000"}]

    def fetch_account_balance(self, account):
        return [{"pdno": "069500", "hldg_qty": "10", "ord_psbl_qty": "7"},
                {"pdno": "153130", "hldg_qty": "0", "ord_psbl_qty": "0"}], self.summary

    def fetch_orderbook(self, ticker):
        return {"bidp1": "85", "askp1": "86"}

    def fetch_etf_quote(self, ticker):
        return {"stck_prpr": "85"}

    def fetch_daily_order_executions(self, account, day):
        return [{"odno": "1", "pdno": "069500", "sll_buy_dvsn_cd": "01", "ord_qty": "10",
                 "tot_ccld_qty": "4", "rmn_qty": "6", "cncl_yn": "N"},
                {"odno": "2", "pdno": "069500", "sll_buy_dvsn_cd": "02", "ord_qty": "5",
                 "tot_ccld_qty": "0", "rmn_qty": "0", "cncl_yn": "Y"}], {}


class Ack:
    accepted = True


class FakeTransport:
    def __init__(self):
        self.calls = []

    def cancel_order(self, **kwargs):
        self.calls.append(kwargs)
        return Ack()


class KisBrokerTests(unittest.TestCase):
    def broker(self, **kw):
        self.transport = FakeTransport()
        return KisBroker(kw.pop("client", FakeClient()), self.transport, account_number="1234567801", **kw)

    def test_maps_balance_quote_and_orders(self):
        b = self.broker()
        self.assertEqual([(p.ticker, p.quantity) for p in b.positions()], [("069500", 10)])
        self.assertEqual(b.sellable_qty("069500"), 7)
        self.assertEqual(b.sellable_qty("999999"), 0)
        self.assertEqual((b.cash_d2(), b.total_equity()), (1000, 5000))
        quote = b.quote("069500")
        self.assertEqual((quote.bid, quote.ask, quote.nav), (85, 86, None))
        self.assertEqual([o.status for o in b.orders(date(2026, 11, 25))], ["PARTIALLY_FILLED", "CANCELED"])

    def test_fill_prices_reuses_the_orders_fetch_once(self):
        """감사 5: orders() 직후 fill_prices() 는 같은 응답을 재사용하고, 그다음 호출은 새로 조회한다."""
        client = FakeClient()
        calls = []
        original = client.fetch_daily_order_executions

        def counting(account, day):
            """조회 횟수를 센다."""
            calls.append(day)
            rows, summary = original(account, day)
            rows[0]["avg_prvs"] = "85"
            return rows, summary
        client.fetch_daily_order_executions = counting
        b, day = self.broker(client=client), date(2026, 11, 25)
        b.orders(day)
        self.assertEqual(b.fill_prices(day), {"1": 85.0})
        self.assertEqual(len(calls), 1)
        b.fill_prices(day)
        self.assertEqual(len(calls), 2)

    def test_missing_cash_field_raises_instead_of_guessing(self):
        b = self.broker(client=FakeClient(summary=[{"tot_evlu_amt": "5000"}]))
        with self.assertRaises(ValueError):
            b.cash_d2()

    def test_cancel_requires_known_order_info(self):
        self.assertFalse(self.broker().cancel_order("1", "069500"))
        b = self.broker(order_lookup=lambda no: ("06010", 6, 85))
        self.assertTrue(b.cancel_order("1", "069500"))
        self.assertEqual(self.transport.calls[0]["quantity"], 6)


if __name__ == "__main__":
    unittest.main()

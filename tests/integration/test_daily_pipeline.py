"""daily_pipeline: 120MA 퇴출·주간 교체·매도 선행·매수 수량·fail-closed 게이트를 가짜 증권사로 검증합니다."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import load_config
from src.daily_pipeline import PipelineConfig, buy_quantity, create_daily_pipeline_handler
from src.db import connect_database, initialize_database
from src.order_manager import Quote
from src.reconciliation import OrderSnapshot, Position
from src.safety_flags import raise_flag
from tests.integration.test_job_handlers import FakeBroker

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 11, 25, 15, 0, tzinfo=KST)  # 수요일 15:00
CFG = PipelineConfig.from_rules(load_config())


class PipelineBroker(FakeBroker):
    """가격·호가·iNAV·매수가능금액을 종목별로 돌려주는 가짜 증권사."""

    def __init__(self, prices, holdings=None, **kw):
        kw.setdefault("cash", 9_000_000)
        kw.setdefault("equity", 10_000_000)
        super().__init__(positions=[Position(t, q) for t, q in (holdings or {}).items()], **kw)
        self.prices, self.buyable, self.raise_buyable = prices, 9_000_000, False

    def last_price(self, ticker):
        return float(self.prices[ticker])

    def quote(self, ticker):
        price = self.prices[ticker]
        return Quote(bid=price, ask=price, nav=float(price))

    def sellable_qty(self, ticker):
        return dict((p.ticker, p.quantity) for p in self._positions).get(ticker, 0)

    def buyable_cash(self):
        if self.raise_buyable:
            raise NotImplementedError("unverified")
        return self.buyable


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = initialize_database(self.path)
        for ticker, cat, region, asset in (("069500", "RISK_ASSET", "KR", "EQUITY"),
                                           ("360750", "RISK_ASSET", "US", "EQUITY"),
                                           ("423160", "SAFE_PARKING", "KR", "MONEY_MARKET")):
            self.conn.execute(
                "INSERT INTO universe_assets (ticker, name, category, region, underlying_index, product_type,"
                " replication_method, status, data_status, added_at, asset_class, classification_verified,"
                " classification_source, verified_by, verified_at, underlying_index_family) VALUES (?,?,?,?,?,"
                "'SPOT_ETF','PHYSICAL','ACTIVE','VALID','t',?,1,'s','op','t',?)",
                (ticker, ticker, cat, region, "IDX" + ticker, asset, "FAM" + ticker))
            for i in range(210):
                self.conn.execute(
                    "INSERT INTO daily_candles (ticker, trade_date, raw_close, adjusted_close, volume, source,"
                    " source_timestamp) VALUES (?,?,100,100,100000000,'KIS','t')",
                    (ticker, f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"))
        self.conn.execute(
            "INSERT INTO daily_portfolio (snapshot_date, total_equity, cash_balance, parking_value, risk_asset_value,"
            " units_outstanding, flow_adjusted_unit_nav, hwm_flow_adjusted, twr_cumulative, twr_quality_status,"
            " benchmark_values, data_quality_status) VALUES ('2026-11-24',10000000,9000000,0,0,100000,100,100,0,"
            "'VALID','{}','VALID')")
        self.conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES ('reconciliation_ok','true','t')")
        self.conn.commit()
        self.now = T0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_preview(self, tickers):
        ranked = [{"ticker": t, "region": "KR" if t == "069500" else "US", "underlying_index": "IDX" + t,
                   "underlying_index_family": "FAM" + t} for t in tickers]
        self.conn.execute(
            "INSERT INTO weekly_previews (preview_id, base_candle_date, ranked_candidates_json,"
            " projected_slots_json, projected_trades_json, status, created_at) VALUES ('w1','2026-11-24',?,'[]','[]',"
            "'SENT',?)", (json.dumps(ranked), (T0 - timedelta(days=4)).isoformat()))
        self.conn.commit()

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)

    def run_pipeline(self, broker):
        create_daily_pipeline_handler(
            lambda: connect_database(self.path), broker, CFG, clock=lambda: self.now, sleep=self.sleep)()

    def calls(self, broker):
        return [(c["ticker"], c["side"].value, c["quantity"]) for c in broker.transport.calls]

    def count(self, sql, *a):
        return self.conn.execute(sql, a).fetchone()[0]


class QuantityTests(unittest.TestCase):
    def test_formula_takes_the_smaller_of_target_and_cash_limits(self):
        common = dict(held_value=0, active_buy_value=0, limit_price=100, cash=5_000_000, active_buy_remaining=0,
                      total_equity=10_000_000, cash_buffer_ratio=0.05, fee_rate=0.0)
        self.assertEqual(buy_quantity(target=4_500_000, buyable=9_000_000, **common), 45_000)
        self.assertEqual(buy_quantity(target=4_500_000, buyable=1_000_000, **common), 5_000)  # (1M-500k)/100
        self.assertEqual(buy_quantity(target=4_500_000, buyable=400_000, **common), 0)       # 버퍼 아래
        self.assertEqual(buy_quantity(target=100, buyable=9_000_000, **{**common, "held_value": 500}), 0)


class CorrelationTests(PipelineTestCase):
    def set_closes(self, ticker, fn):
        rows = self.conn.execute("SELECT trade_date FROM daily_candles WHERE ticker = ? ORDER BY trade_date",
                                 (ticker,)).fetchall()
        for index, row in enumerate(rows):
            self.conn.execute("UPDATE daily_candles SET adjusted_close = ? WHERE ticker = ? AND trade_date = ?",
                              (fn(index), ticker, row[0]))
        self.conn.commit()

    def test_identical_series_correlate_and_independent_series_do_not(self):
        from src.daily_pipeline import db_correlation
        self.set_closes("069500", lambda i: 100 + (i % 5))
        self.set_closes("360750", lambda i: 100 + (i % 5))
        self.assertAlmostEqual(db_correlation(self.conn)("069500", "360750"), 1.0)
        self.set_closes("360750", lambda i: 100 + ((i * 7) % 11))
        self.assertLess(abs(db_correlation(self.conn)("069500", "360750")), 0.5)

    def test_flat_or_short_history_returns_none(self):
        from src.daily_pipeline import db_correlation
        self.assertIsNone(db_correlation(self.conn)("069500", "360750"))  # 상수 시계열(분산 0)


class PipelineTests(PipelineTestCase):
    def test_rotation_day_buys_top_ranked_then_parking_and_consumes_preview(self):
        self.add_preview(["069500", "360750"])
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100})
        self.run_pipeline(broker)
        tickers = [c[0] for c in self.calls(broker)]
        self.assertEqual(tickers[:2], ["069500", "360750"])
        self.assertIn("423160", tickers)
        self.assertEqual(self.conn.execute("SELECT status FROM weekly_previews").fetchone()[0], "CONSUMED")
        self.assertEqual(self.count("SELECT COUNT(*) FROM portfolio_slots WHERE ticker IS NOT NULL"), 2)

    def test_non_rotation_day_only_parks_cash(self):
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100})
        self.run_pipeline(broker)
        self.assertEqual({c[0] for c in self.calls(broker)}, {"423160"})

    def test_ma120_exit_sells_and_blocks_reentry(self):
        self.add_preview(["069500"])
        broker = PipelineBroker({"069500": 90, "360750": 110, "423160": 100}, holdings={"069500": 10})
        broker._orders = [OrderSnapshot("0000001", "069500", "SELL", 10, 10, 0, "FILLED")]
        self.run_pipeline(broker)
        self.assertEqual([c for c in self.calls(broker) if c[1] == "SELL"], [("069500", "SELL", 10)])
        self.assertNotIn(("069500", "BUY"), [(c[0], c[1]) for c in self.calls(broker)])

    def test_rotation_sells_holding_missing_from_final_slots_before_any_buy(self):
        self.add_preview(["360750"])
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100}, holdings={"069500": 10})
        broker._orders = [OrderSnapshot("0000001", "069500", "SELL", 10, 10, 0, "FILLED")]
        self.run_pipeline(broker)
        sides = [c[1] for c in self.calls(broker)]
        self.assertEqual(sides[0], "SELL")
        self.assertIn("BUY", sides[1:])

    def test_unfinished_sell_skips_buy_phase_after_deadline(self):
        self.add_preview(["360750"])
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100}, holdings={"069500": 10})
        self.run_pipeline(broker)  # 매도가 체결 통보 없이 ACCEPTED 로 남음
        self.assertEqual([c[1] for c in self.calls(broker)], ["SELL"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM system_events WHERE event_type='BUY_PHASE_SKIPPED'"), 1)

    def test_buy_block_flag_and_unverified_cash_tr_fail_closed(self):
        self.add_preview(["069500"])
        raise_flag(self.conn, "BUY_BLOCK", "UNCLASSIFIED_CASH_FLOW", now=T0)
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100})
        self.run_pipeline(broker)
        self.assertEqual(broker.transport.calls, [])
        self.conn.execute("UPDATE safety_flags SET cleared_at = 't'")
        self.conn.commit()
        broker.raise_buyable = True
        self.run_pipeline(broker)
        self.assertEqual(broker.transport.calls, [])

    def test_safe_halt_skips_everything(self):
        raise_flag(self.conn, "HALT", "PENDING_CONFIRMATION", now=T0)
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100})
        self.run_pipeline(broker)
        self.assertEqual(broker.transport.calls, [])

    def test_drift_overweight_is_trimmed_down_to_target_with_floor(self):
        self.add_preview(["069500"])
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100}, holdings={"069500": 100_000})
        self.run_pipeline(broker)
        sells = [c for c in self.calls(broker) if c[1] == "SELL"]
        self.assertEqual(sells, [("069500", "SELL", 59_090)])  # floor((11,000,000 - 4,500,000) / 110)

    def test_parking_is_liquidated_only_for_the_cash_shortfall(self):
        self.add_preview(["069500"])
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100}, holdings={"423160": 50_000},
                                cash=100_000)
        broker.buyable = 100_000
        self.run_pipeline(broker)
        self.assertEqual([c for c in self.calls(broker) if c[1] == "SELL"], [("423160", "SELL", 45_000)])

    def test_stop_loss_reentry_block_is_released_by_the_next_consumed_rotation(self):
        self.add_preview(["069500"])
        self.conn.execute("UPDATE universe_assets SET blocked_until_next_rotation = 1, blocked_since = '2026-11-20' "
                          "WHERE ticker = '360750'")
        self.conn.commit()
        self.run_pipeline(PipelineBroker({"069500": 110, "360750": 110, "423160": 100}))
        self.assertEqual(self.conn.execute(
            "SELECT blocked_until_next_rotation FROM universe_assets WHERE ticker = '360750'").fetchone()[0], 0)

    def test_blocked_or_illiquid_candidates_fail_live_filters(self):
        self.add_preview(["360750", "069500"])
        self.conn.execute("UPDATE universe_assets SET blocked_until_next_rotation = 1 WHERE ticker = '360750'")
        self.conn.commit()
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100})
        self.run_pipeline(broker)
        self.assertNotIn("360750", [c[0] for c in self.calls(broker)])
        self.assertIn("069500", [c[0] for c in self.calls(broker)])

    def test_missing_nav_blocks_strategy_orders(self):
        self.add_preview(["069500"])
        broker = PipelineBroker({"069500": 110, "360750": 110, "423160": 100})
        broker.quote = lambda t: Quote(bid=110, ask=110, nav=None)
        self.run_pipeline(broker)
        self.assertEqual(broker.transport.calls, [])


if __name__ == "__main__":
    unittest.main()

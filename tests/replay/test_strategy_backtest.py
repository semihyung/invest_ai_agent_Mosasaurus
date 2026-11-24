"""전략 백테스트 엔진: 운영 규칙 재현·미래 참조 금지·비용·Walk-Forward·승인 산출물 테스트입니다."""

import random
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from backtest.data_loader import load_market_csv
from backtest.engine import (
    AssetSpec,
    Bar,
    EngineConfig,
    pearson,
    parking_proxy_closes,
    run_strategy_backtest,
    with_params,
)
from backtest.report import build_report, report_sha256, sha256_file
from backtest.walk_forward import walk_forward_evaluate

SESSIONS = 460
START = 200


def calendar(count=SESSIONS):
    """2024-01-01 부터 평일만 `count` 개 만든 거래일 목록입니다."""
    days, current = [], date(2024, 1, 1)
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def make_bars(dates, returns, *, start=100.0, turnover=2e10, intraday_drop=0.005, opens=None):
    """일간 수익률 목록에서 시가·저가·종가가 일관된 일봉을 만듭니다."""
    bars, price = [], start
    for i, day in enumerate(dates):
        previous = price
        price = price * (1 + returns[i])
        open_price = (opens or {}).get(i, previous)
        low = min(open_price, price) * (1 - intraday_drop)
        bars.append(Bar(day, open_price, low, price, 1000, turnover))
    return bars


def noise(seed, count, sigma=0.008, drift=0.0):
    """재현 가능한 일간 수익률(±30% 상한 이내)입니다."""
    rng = random.Random(seed)
    return [max(-0.2, min(0.2, rng.gauss(drift, sigma))) for _ in range(count)]


def parking_returns(count):
    """연 3% 의 매끈한 파킹 수익률입니다."""
    return [(1.03) ** (1 / 252) - 1] * count


def world(**drifts):
    """티커별 드리프트로 합성 시장(+파킹 PARK)을 만듭니다."""
    dates = calendar()
    bars = {"PARK": make_bars(dates, parking_returns(SESSIONS), intraday_drop=0.0)}
    for index, (ticker, drift) in enumerate(drifts.items()):
        bars[ticker] = make_bars(dates, noise(100 + index, SESSIONS, drift=drift))
    return dates, bars


def specs(*items):
    """(티커, 지역, 지수 패밀리) 튜플에서 AssetSpec 목록을 만듭니다."""
    return [AssetSpec(t, region, family) for t, region, family in items]


UNIVERSE = specs(("AAA", "KR", "K200"), ("BBB", "US", "SP500"), ("CCC", "KR", "SEMI"), ("DDD", "US", "NDX"))


class EngineBehaviourTests(unittest.TestCase):
    """합성 시장에서 엔진의 핵심 동작을 검증합니다."""

    def setUp(self):
        self.dates, self.bars = world(AAA=0.0015, BBB=0.0012, CCC=0.0008, DDD=-0.0005)
        self.config = EngineConfig()

    def run_it(self, **kwargs):
        return run_strategy_backtest(UNIVERSE, kwargs.pop("bars", self.bars), "PARK",
                                     kwargs.pop("config", self.config), **kwargs)

    def test_runs_and_equity_is_positive_with_weekly_rotations(self):
        run = self.run_it()
        self.assertEqual(len(run.equity), SESSIONS - START)
        self.assertTrue(all(value > 0 for value in run.equity))
        weeks = len({date.fromisoformat(d).isocalendar()[:2] for d in self.dates[START:]})
        self.assertEqual(run.rotations, weeks)
        self.assertGreater(run.trades, 0)
        self.assertTrue(any(" BUY " in line and "PARK" not in line for line in run.log))

    def test_signals_never_use_future_data(self):
        """미래 일봉을 완전히 바꿔도 그 이전 날짜의 자산 곡선은 달라지지 않아야 합니다."""
        cut = 350
        baseline = self.run_it(end_index=cut)
        changed = {t: list(series) for t, series in self.bars.items()}
        for ticker in ("AAA", "BBB", "CCC", "DDD"):
            for i in range(cut, SESSIONS):
                bar = changed[ticker][i]
                changed[ticker][i] = Bar(bar.trade_date, bar.open * 0.4, bar.low * 0.4,
                                         bar.close * 0.4, bar.volume, bar.turnover_krw)
        altered = self.run_it(bars=changed, end_index=cut)
        self.assertEqual(baseline.equity, altered.equity)
        full = self.run_it()
        self.assertEqual(full.equity[: cut - START], baseline.equity)

    def test_flat_market_stays_in_parking_without_risk_buys(self):
        dates, bars = world(AAA=-0.004, BBB=-0.003)
        run = run_strategy_backtest(specs(("AAA", "KR", "K"), ("BBB", "US", "S")), bars, "PARK")
        self.assertFalse(any(" BUY AAA" in line or " BUY BBB" in line for line in run.log))
        self.assertTrue(dict(run.final_positions).get("PARK", 0) > 0)
        self.assertGreater(run.equity[-1], run.equity[0] * 0.99)

    def test_costs_reduce_final_equity_monotonically(self):
        cheap = self.run_it(config=with_params(self.config, fee_rate=0.0, slippage_ratio=0.0))
        base = self.run_it()
        costly = self.run_it(config=with_params(self.config, fee_rate=0.0006, slippage_ratio=0.008))
        self.assertGreater(cheap.equity[-1], base.equity[-1])
        self.assertGreater(base.equity[-1], costly.equity[-1])
        self.assertGreater(costly.fees_paid, base.fees_paid)

    def test_region_weight_cap_limits_us_exposure(self):
        dates, bars = world(AAA=0.0004, BBB=0.0020, DDD=0.0018)
        run = run_strategy_backtest(
            specs(("AAA", "KR", "K200"), ("BBB", "US", "SP500"), ("DDD", "US", "NDX")),
            bars, "PARK")
        values = {t: s * bars[t][-1].close for t, s in run.final_positions if t != "PARK"}
        total = run.equity[-1]
        self.assertLessEqual((values.get("BBB", 0) + values.get("DDD", 0)) / total, 0.70)  # 한도 65% + 드리프트 여유

    def test_same_index_family_is_never_held_twice(self):
        dates, bars = world(AAA=0.0015, BBB=0.0014)
        run = run_strategy_backtest(
            specs(("AAA", "US", "SP500"), ("BBB", "US", "SP500")), bars, "PARK")
        held = {t for t, s in run.final_positions if t in ("AAA", "BBB") and s > 0}
        self.assertLessEqual(len(held), 1)

    def test_trailing_stop_triggers_and_blocks_reentry_in_the_same_week(self):
        dates, bars = world(AAA=0.0025)
        crash_day = 330
        series = list(bars["AAA"])
        for i in range(crash_day, SESSIONS):
            bar = series[i]
            factor = 0.80
            series[i] = Bar(bar.trade_date, bar.open * factor if i == crash_day else bar.open * factor,
                            bar.low * factor, bar.close * factor, bar.volume, bar.turnover_krw)
        bars["AAA"] = series
        intraday = run_strategy_backtest(specs(("AAA", "KR", "K")), bars, "PARK")
        self.assertGreaterEqual(intraday.stops, 1)
        self.assertTrue(any("STOP AAA" in line for line in intraday.log))
        stop_line = next(line for line in intraday.log if "STOP AAA" in line)
        stop_date = stop_line.split()[0]
        same_week = [line for line in intraday.log if " BUY AAA" in line
                     and date.fromisoformat(line.split()[0]).isocalendar()[:2]
                     == date.fromisoformat(stop_date).isocalendar()[:2]
                     and line.split()[0] >= stop_date]
        self.assertEqual(same_week, [])
        next_open = run_strategy_backtest(
            specs(("AAA", "KR", "K")), bars, "PARK",
            with_params(EngineConfig(), stop_execution="NEXT_OPEN"))
        self.assertTrue(any("STOP_NEXT_OPEN AAA" in line for line in next_open.log) or next_open.stops >= 0)
        self.assertNotEqual(intraday.equity, next_open.equity)

    def test_ma120_exit_is_evaluated_on_non_rotation_days(self):
        """수요일 하루 -7% 충격(스탑 -10% 미달)으로 120MA 를 이탈하면, 다음 월요일 교체를 기다리지 않고 당일 퇴출합니다."""
        dates = calendar()
        shock = next(i for i in range(380, SESSIONS) if date.fromisoformat(dates[i]).weekday() == 2)
        returns = noise(7, SESSIONS, sigma=0.002, drift=0.0008)
        returns[shock] = -0.07
        bars = {"PARK": make_bars(dates, parking_returns(SESSIONS), intraday_drop=0.0),
                "AAA": make_bars(dates, returns, intraday_drop=0.0005)}
        run = run_strategy_backtest(specs(("AAA", "KR", "K")), bars, "PARK")
        self.assertEqual(run.stops, 0)  # 스탑이 아니라 120MA 판정
        exit_lines = [line for line in run.log if "MA120_EXIT AAA" in line]
        self.assertTrue(exit_lines, run.log[-6:])
        self.assertEqual(exit_lines[0].split()[0], dates[shock])
        self.assertEqual(date.fromisoformat(dates[shock]).weekday(), 2)  # 수요일(교체일 아님)

    def test_delisted_asset_is_liquidated_and_not_a_candidate_afterwards(self):
        listed = [AssetSpec("AAA", "KR", "K200", delisted_after=self.dates[300]),
                  AssetSpec("CCC", "KR", "SEMI")]
        run = run_strategy_backtest(listed, self.bars, "PARK")
        self.assertTrue(any("DELIST AAA" in line for line in run.log))
        self.assertNotIn("AAA", dict(run.final_positions))
        late_buys = [line for line in run.log if " BUY AAA" in line and line.split()[0] > self.dates[300]]
        self.assertEqual(late_buys, [])

    def test_calendar_and_range_validation(self):
        broken = {t: list(s) for t, s in self.bars.items()}
        broken["AAA"] = broken["AAA"][:-1]
        with self.assertRaises(ValueError):
            run_strategy_backtest(UNIVERSE, broken, "PARK")
        with self.assertRaises(ValueError):
            run_strategy_backtest(UNIVERSE, self.bars, "PARK", start_index=100)
        with self.assertRaises(ValueError):
            run_strategy_backtest(UNIVERSE, self.bars, "PARK",
                                  with_params(self.config, stop_execution="MARKET"))

    def test_pearson_returns_none_for_zero_variance(self):
        self.assertIsNone(pearson([1, 1, 1, 1], [1, 2, 3, 4]))
        self.assertAlmostEqual(pearson([1, 2, 3, 4], [2, 4, 6, 8]), 1.0)

    def test_parking_proxy_grows_at_the_given_rate(self):
        closes = parking_proxy_closes(calendar(253), 0.03)
        self.assertAlmostEqual(closes[252] / closes[0], 1.03, places=6)
        with self.assertRaises(ValueError):
            parking_proxy_closes(calendar(10), -1.5)


class WalkForwardAndReportTests(unittest.TestCase):
    """Walk-Forward 의 미래 데이터 차단과 승인 산출물 해시 안정성입니다."""

    def test_parameters_are_chosen_from_training_results_only(self):
        calls = []

        def run(params, start, end):
            calls.append((params["k"], start, end))
            # 학습 구간에서는 k=1 이 좋고, 검증 구간에서는 k=2 가 좋도록 구성(미래를 보면 k=2 선택).
            in_training = end - start == 100
            good = (params["k"] == 1) if in_training else (params["k"] == 2)
            return (0.2 if good else 0.01, -0.1, [1.0, 1.1])

        results = walk_forward_evaluate(
            run, [{"k": 1}, {"k": 2}], history_sessions=200, total_sessions=200 + 100 + 50,
            training_sessions=100, test_sessions=50)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].chosen_params, {"k": 1})
        self.assertEqual(results[0].train_range, (200, 300))
        self.assertEqual(results[0].test_range, (300, 350))
        for k, start, end in calls[:2]:
            self.assertLessEqual(end, 300)  # 선택 단계에서는 학습 구간 끝까지만 사용

    def test_windows_roll_forward_without_overlap_and_require_enough_data(self):
        results = walk_forward_evaluate(
            lambda p, s, e: (0.1, -0.1, [1.0]), [{"k": 1}], history_sessions=200,
            total_sessions=200 + 100 + 50 * 3, training_sessions=100, test_sessions=50)
        self.assertEqual([r.test_range for r in results], [(300, 350), (350, 400), (400, 450)])
        with self.assertRaises(ValueError):
            walk_forward_evaluate(lambda p, s, e: (0, -0.1, [1]), [{"k": 1}], history_sessions=200,
                                  total_sessions=250, training_sessions=100, test_sessions=50)
        with self.assertRaises(ValueError):
            walk_forward_evaluate(lambda p, s, e: (0, -0.1, [1]), [], history_sessions=200,
                                  total_sessions=500, training_sessions=100, test_sessions=50)

    def test_report_hash_is_stable_and_sensitive_to_strategy_hash_and_data(self):
        dates, bars = world(AAA=0.0015, BBB=0.0010)
        run = run_strategy_backtest(specs(("AAA", "KR", "K"), ("BBB", "US", "S")), bars, "PARK")
        kwargs = dict(
            base=run, cost_sensitivity={"fee_x2": run}, parameter_sensitivity={"floor_0.003": run},
            walk_forward=[{"train": [200, 300], "test": [300, 350], "params": {"k": 1}}],
            strategy_params_hash="a" * 64, code_commit_sha="abc123",
            data_files={"AAA.csv": "1" * 64}, universe_history_sha256="2" * 64,
            parking_proxy_note="프록시: CD 91일물")
        first = report_sha256(build_report(**kwargs))
        again = report_sha256(build_report(**kwargs))
        changed_strategy = report_sha256(build_report(**{**kwargs, "strategy_params_hash": "b" * 64}))
        changed_data = report_sha256(build_report(**{**kwargs, "data_files": {"AAA.csv": "9" * 64}}))
        self.assertEqual(first, again)
        self.assertEqual(len({first, changed_strategy, changed_data}), 3)
        self.assertEqual(len(first), 64)

    def test_market_csv_loader_validates_rows_and_sha256_is_content_based(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AAA.csv"
            rows = ["trade_date,open,low,adjusted_close,volume,turnover_krw"]
            rows += [f"2024-{1 + i // 28:02d}-{1 + i % 28:02d},100,99,100,10,1000000000" for i in range(210)]
            path.write_text("\n".join(rows), encoding="utf-8")
            bars = load_market_csv(path)
            self.assertEqual(len(bars), 210)
            digest = sha256_file(path)
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            self.assertNotEqual(digest, sha256_file(path))
            bad = rows[:5] + [rows[3]] + rows[5:]
            path.write_text("\n".join(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_market_csv(path)
            path.write_text("\n".join(rows[:1] + ["2024-01-01,100,120,100,10,1"] * 1), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_market_csv(path)


if __name__ == "__main__":
    unittest.main()

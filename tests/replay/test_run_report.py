"""승인용 백테스트 보고서 실행기: CSV → 보고서 JSON → `/approve_backtest` 가 검증하는 해시까지 이어지는지 검사합니다."""

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from backtest.run_report import engine_config_from_rules, generate_report, load_universe_history, write_report
from src.config import load_config, strategy_params_hash
from tests.replay.test_strategy_backtest import world


def write_world(directory: Path) -> Path:
    """합성 시장(AAA·BBB·CCC·DDD·PARK)을 CSV 와 유니버스 이력 YAML 로 저장하고 YAML 경로를 돌려준다."""
    dates, bars = world(AAA=0.0015, BBB=0.0012, CCC=0.0008, DDD=-0.0005)
    for ticker, series in bars.items():
        with (directory / f"{ticker}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["trade_date", "open", "low", "adjusted_close", "volume", "turnover_krw"])
            for bar in series:
                writer.writerow([bar.trade_date, bar.open, bar.low, bar.close, bar.volume, bar.turnover_krw])
    universe = {
        "parking_ticker": "PARK", "parking_proxy_note": "테스트: 파킹은 연 3% 합성 지수",
        "assets": [{"ticker": t, "region": r, "index_family": f}
                   for t, r, f in (("AAA", "KR", "K200"), ("BBB", "US", "SP500"), ("CCC", "KR", "SEMI"),
                                   ("DDD", "US", "NDX"))],
    }
    path = directory / "universe.yaml"
    path.write_text(yaml.safe_dump(universe), encoding="utf-8")
    return path


class RunReportTests(unittest.TestCase):
    """보고서 생성·저장·해시 검증."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.universe = write_world(self.dir)
        self.rules = load_config()

    def build(self):
        return generate_report(data_dir=self.dir, universe_history=self.universe, rules=self.rules,
                               code_commit_sha="a" * 40, training_sessions=100, test_sessions=60)

    def test_report_has_every_section_and_binds_the_current_strategy(self):
        report = self.build()
        self.assertEqual(report["strategy_params_hash"], strategy_params_hash(self.rules))
        self.assertEqual(report["code_commit_sha"], "a" * 40)
        self.assertEqual(set(report["cost_sensitivity"]), {"fee_x2", "slippage_x2", "slippage_x3"})
        self.assertEqual(len(report["parameter_sensitivity"]), 4)
        self.assertGreaterEqual(len(report["walk_forward"]), 1)
        self.assertEqual(sorted(report["data_files_sha256"]), ["AAA.csv", "BBB.csv", "CCC.csv", "DDD.csv", "PARK.csv"])
        self.assertIn("합성 지수", report["assumptions"][-1])
        # 더 높은 수수료는 같은 전략의 성과를 낮춘다(비용 민감도가 실제로 엔진에 반영됨)
        self.assertLessEqual(report["cost_sensitivity"]["fee_x2"]["cagr"], report["base"]["cagr"])

    def test_saved_file_hashes_exactly_like_the_telegram_approval_check(self):
        report = self.build()
        run_id, digest, path = write_report(report, self.dir / "out")
        self.assertEqual(path.name, f"{run_id}.json")
        # telegram_actions.approve_backtest 가 하는 계산: 파일을 읽어 정규화 JSON 의 SHA-256 을 낸다.
        loaded = json.loads(path.read_text(encoding="utf-8"))
        canonical = json.dumps(loaded, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.assertEqual(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), digest)

    def test_run_id_must_match_the_approval_command_format(self):
        report = self.build()
        for bad in ("ab", "UPPER_case", "has space", "x" * 65):
            with self.assertRaises(ValueError):
                write_report(report, self.dir / "out", bad)

    def test_universe_history_requires_a_parking_proxy_note(self):
        broken = self.dir / "broken.yaml"
        broken.write_text(yaml.safe_dump({"parking_ticker": "PARK", "assets": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_universe_history(broken)

    def test_engine_config_reads_the_shipped_rules(self):
        config = engine_config_from_rules(self.rules)
        self.assertEqual(config.stop_normal, -0.10)
        self.assertEqual(config.slot_weights, (0.45, 0.30, 0.20))
        self.assertEqual(config.hold_rank_buffer, 5)


if __name__ == "__main__":
    unittest.main()

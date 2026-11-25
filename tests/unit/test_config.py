import tempfile
import unittest
from pathlib import Path

from src.config import ConfigError, load_config


class ConfigLoaderTests(unittest.TestCase):
    """YAML 설정 파서와 환경 변수 치환을 검증합니다."""

    def test_loads_nested_yaml_and_expands_environment_values(self):
        """중첩 문자열과 목록 내부의 환경 변수 참조를 치환합니다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.yaml"
            config_path.write_text(
                "models:\n  fast: ${MODEL_NAME}\nitems:\n  - ${MODEL_NAME}\n"
                "limits:\n  count: 3\n",
                encoding="utf-8",
            )

            config = load_config(config_path, environ={"MODEL_NAME": "fake-model"})

        self.assertEqual(config["models"]["fast"], "fake-model")
        self.assertEqual(config["items"], ["fake-model"])
        self.assertEqual(config["limits"]["count"], 3)

    def test_missing_environment_variable_fails_without_exposing_values(self):
        """필수 환경 변수가 없으면 설정 로딩을 중단합니다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.yaml"
            config_path.write_text("api_key: ${MISSING_API_KEY}\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "MISSING_API_KEY"):
                load_config(config_path, environ={})

    def test_rejects_non_mapping_root(self):
        """최상위 YAML 값이 매핑이 아니면 거부합니다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.yaml"
            config_path.write_text("- item\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "root"):
                load_config(config_path, environ={})

    def test_rejects_invalid_yaml(self):
        """문법이 잘못된 YAML을 설정 오류로 변환합니다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.yaml"
            config_path.write_text("items: [\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "Invalid YAML"):
                load_config(config_path, environ={})

    def test_default_rules_require_v374_product_classification(self):
        """기본 설정이 v3.7.2 분류 게이트와 Allowlist 경로를 강제합니다."""
        config = load_config(environ={})

        self.assertEqual(config["version"], "3.7.5")
        self.assertEqual(
            config["universe"]["allowlist_file"],
            "config/universe_allowlist.yaml",
        )
        self.assertTrue(config["universe"]["require_verified_classification"])
        self.assertTrue(config["universe"]["forbid_name_keyword_heuristic"])


if __name__ == "__main__":
    unittest.main()


class CodeConstantParityTests(unittest.TestCase):
    """감사 4번: 설정에 적힌 값이 코드가 실제로 쓰는 상수와 같은지 확인하고, 어긋나면 부팅을 거부한다."""

    def test_shipped_rules_pass_and_a_changed_stop_is_rejected(self):
        import copy

        from src.config import ConfigError, load_config, validate_config

        config = load_config()
        validate_config(config)
        changed = copy.deepcopy(config)
        changed["trailing_stop"]["normal"] = -0.08  # 설정만 바꿔도 코드는 -10% 에서 판다
        with self.assertRaisesRegex(ConfigError, "trailing_stop.normal must equal"):
            validate_config(changed)

    def test_phase2_flags_are_rejected_while_phase2_is_not_implemented(self):
        import copy

        from src.config import ConfigError, load_config, validate_config

        config = copy.deepcopy(load_config())
        config["features"]["enable_llm_risk_engine"] = True
        with self.assertRaisesRegex(ConfigError, "enable_llm_risk_engine must be false"):
            validate_config(config)

    def test_every_pinned_value_matches_the_code(self):
        from src import cash_recon, db, market_data, net_watchdog, reconciliation, order_executor, order_manager, safety_flags, telegram_bot
        from src.config import _CODE_CONSTANTS
        from src.risk_manager import get_drawdown_mode, next_drawdown_stage, trailing_stop_ratio
        import inspect

        actual = {
            "trailing_stop.normal": trailing_stop_ratio(False),
            "trailing_stop.training": trailing_stop_ratio(True),
            "risk.portfolio_drawdown.caution": inspect.signature(get_drawdown_mode).parameters["caution_threshold"].default,
            "risk.portfolio_drawdown.warning": inspect.signature(get_drawdown_mode).parameters["warning_threshold"].default,
            "risk.portfolio_drawdown.critical": inspect.signature(get_drawdown_mode).parameters["critical_threshold"].default,
            "risk.portfolio_drawdown.recovery_buffer": inspect.signature(next_drawdown_stage).parameters["recovery_buffer"].default,
            "execution.max_spread_ratio": order_manager.MAX_SPREAD_RATIO,
            "execution.max_spread_ratio_reduce_only": order_executor.MAX_SPREAD_REDUCE_ONLY,
            "execution.nav_deviation_limit_kr": order_manager.NAV_DEVIATION_LIMITS["KR"],
            "execution.nav_deviation_limit_us": order_manager.NAV_DEVIATION_LIMITS["US"],
            "execution.nav_deviation_limit_global": order_manager.NAV_DEVIATION_LIMITS["GLOBAL"],
            "execution.reduce_only_nav_floor_ratio": order_manager.REDUCE_ONLY_NAV_FLOOR_RATIO,
            "execution.cancel_confirm_consecutive_matches": inspect.signature(order_manager.cancel_can_finalize).parameters["required_matches"].default,
            "ttl_seconds.reconciliation": order_executor.RECONCILIATION_TTL_SECONDS,
            "telegram.confirm_code_length": telegram_bot.CONFIRM_CODE_LENGTH,
            "telegram.confirm_max_failures": telegram_bot.CONFIRM_MAX_FAILURES,
            "telegram.confirm_lockout_seconds": int(telegram_bot.CONFIRM_LOCKOUT.total_seconds()),
            "telegram.max_commands_per_minute": telegram_bot.MAX_COMMANDS_PER_MINUTE,
            "db.busy_timeout_ms": db._BUSY_TIMEOUT_MS,
            "db.max_write_tx_ms": int(db._MAX_WRITE_TX_SECONDS * 1000),
            "host_state.run_file_ttl_seconds": inspect.signature(safety_flags.sync_host_state_flags).parameters["ttl_seconds"].default,
            "ups.halt_ratio": inspect.signature(safety_flags.sync_host_state_flags).parameters["ups_halt_ratio"].default,
            "ups.clear_ratio_margin": inspect.signature(safety_flags.sync_host_state_flags).parameters["ups_clear_margin"].default,
            "net_watchdog.max_reboots_per_hour": inspect.signature(net_watchdog.decide_action).parameters["max_reboots_per_hour"].default,
            "accounting.cash_tolerance_krw": inspect.signature(cash_recon.unexplained_cash_delta).parameters["tolerance_krw"].default,
            "accounting.recon_confirm_interval_seconds": inspect.signature(reconciliation.evaluate_account_mismatch).parameters["confirm_interval_seconds"].default,
            "execution.late_fill_grace_seconds": inspect.signature(cash_recon.reconciliation_allowed).parameters["grace_seconds"].default,
            "ttl_seconds.intraday_quote": inspect.signature(market_data.validate_quote).parameters["ttl_seconds"].default,
            "clock.max_offset_seconds": inspect.signature(safety_flags.sync_host_state_flags).parameters["clock_max_offset_seconds"].default,
            "execution.new_order_cutoff": order_executor.ORDER_WINDOW[1].strftime("%H:%M:%S"),
            "market.regular_open": order_executor.ORDER_WINDOW[0].strftime("%H:%M:%S"),
        }
        for key, value in actual.items():
            self.assertEqual(_CODE_CONSTANTS[key], value, key)

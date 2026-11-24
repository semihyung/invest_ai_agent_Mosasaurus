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

        self.assertEqual(config["version"], "3.7.4")
        self.assertEqual(
            config["universe"]["allowlist_file"],
            "config/universe_allowlist.yaml",
        )
        self.assertTrue(config["universe"]["require_verified_classification"])
        self.assertTrue(config["universe"]["forbid_name_keyword_heuristic"])


if __name__ == "__main__":
    unittest.main()

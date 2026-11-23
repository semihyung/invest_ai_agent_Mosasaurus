import tempfile
import unittest
from pathlib import Path

from src.db import initialize_database
from src.universe import (
    ProductMetadata,
    load_universe_allowlist,
    sync_universe_allowlist,
    validate_product_metadata,
)


class UniverseClassificationTests(unittest.TestCase):
    """공식 상품 분류와 운영자 승인 기록의 fail-closed 동작을 검사합니다."""

    def setUp(self):
        """테스트 전용 임시 디렉터리를 준비합니다."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.allowlist_path = Path(self.temp_dir.name) / "allowlist.yaml"

    def _valid_entry(self):
        """운영자 승인 형식에 맞는 가짜 ETF 메타데이터를 반환합니다."""
        return (
            "    - ticker: '123450'\n"
            "      name: Sample ETF\n"
            "      category: RISK_ASSET\n"
            "      region: KR\n"
            "      underlying_index: Sample Index\n"
            "      product_type: SPOT_ETF\n"
            "      replication_method: PHYSICAL\n"
            "      leverage_multiplier: 1.0\n"
            "      is_inverse: false\n"
            "      is_futures_based: false\n"
            "      classification_source: KIS_PRODUCT_MASTER\n"
            "      verified_by: test-operator\n"
            "      verified_at: '2026-01-01T00:00:00Z'\n"
        )

    def test_empty_allowlist_is_valid_and_safe(self):
        """초기 승인 종목이 없으면 빈 목록을 반환합니다."""
        self.allowlist_path.write_text(
            'version: "3.7.2"\nassets: []\n',
            encoding="utf-8",
        )

        self.assertEqual(load_universe_allowlist(self.allowlist_path), [])

    def test_allowlist_sync_is_idempotent_and_records_provenance(self):
        """승인 종목을 한 번만 추가하고 공식 출처와 운영자를 남깁니다."""
        self.allowlist_path.write_text(
            'version: "3.7.2"\nassets:\n' + self._valid_entry(),
            encoding="utf-8",
        )
        assets = load_universe_allowlist(self.allowlist_path)
        connection = initialize_database(
            Path(self.temp_dir.name) / "mosasaurus.db"
        )
        self.addCleanup(connection.close)

        self.assertEqual(sync_universe_allowlist(connection, assets), 1)
        self.assertEqual(sync_universe_allowlist(connection, assets), 0)
        row = connection.execute(
            """
            SELECT classification_verified, classification_source, verified_by,
                   verified_at, leverage_multiplier, is_inverse, is_futures_based
            FROM universe_assets WHERE ticker = '123450'
            """
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                1,
                "KIS_PRODUCT_MASTER",
                "allowlist:test-operator",
                "2026-01-01T00:00:00+00:00",
                1.0,
                0,
                0,
            ),
        )

    def test_allowlist_rejects_leverage_and_keyword_names_are_not_used(self):
        """구조화된 배수 필드로 레버리지를 거부하고 이름은 판정에 쓰지 않습니다."""
        leveraged = ProductMetadata(
            ticker="123450",
            name="Plain Name",
            region="KR",
            underlying_index="Sample Index",
            product_type="SPOT_ETF",
            replication_method="PHYSICAL",
            leverage_multiplier=2.0,
            is_inverse=False,
            is_futures_based=False,
            classification_source="KIS_PRODUCT_MASTER",
        )
        with self.assertRaisesRegex(ValueError, "배수"):
            validate_product_metadata(
                leveraged,
                expected_ticker="123450",
            )

        name_with_keyword = ProductMetadata(
            ticker="123450",
            name="레버리지라는 문자열만 포함한 일반 ETF",
            region="KR",
            underlying_index="Sample Index",
            product_type="SPOT_ETF",
            replication_method="PHYSICAL",
            leverage_multiplier=1.0,
            is_inverse=False,
            is_futures_based=False,
            classification_source="KIS_PRODUCT_MASTER",
        )
        validate_product_metadata(
            name_with_keyword,
            expected_ticker="123450",
        )

    def test_synthetic_replication_is_allowed_only_for_approved_parking(self):
        """합성복제는 지정 파킹 티커가 아닌 경우 거부합니다."""
        metadata = ProductMetadata(
            ticker="123450",
            name="Sample ETF",
            region="KR",
            underlying_index="Sample Index",
            product_type="SPOT_ETF",
            replication_method="SYNTHETIC",
            leverage_multiplier=1.0,
            is_inverse=False,
            is_futures_based=False,
            classification_source="KRX_ETF_MASTER",
        )

        with self.assertRaisesRegex(ValueError, "합성복제"):
            validate_product_metadata(
                metadata,
                expected_ticker="123450",
                category="SAFE_PARKING",
                allowed_synthetic_parking_tickers=("423160",),
            )

        validate_product_metadata(
            ProductMetadata(
                ticker="423160",
                name="Sample Parking ETF",
                region="KR",
                underlying_index="Sample Index",
                product_type="SPOT_ETF",
                replication_method="SYNTHETIC",
                leverage_multiplier=1.0,
                is_inverse=False,
                is_futures_based=False,
                classification_source="KRX_ETF_MASTER",
            ),
            expected_ticker="423160",
            category="SAFE_PARKING",
            allowed_synthetic_parking_tickers=("423160",),
        )


if __name__ == "__main__":
    unittest.main()

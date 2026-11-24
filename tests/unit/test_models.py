import unittest

from src import models
from src.order_manager import OrderStatus
from src.risk_manager import OperatingMode


class ModelsExportTests(unittest.TestCase):
    """공용 모델 모듈이 원본 정의를 그대로 노출하는지 검증합니다."""

    def test_models_reexport_single_source_definitions(self):
        """주문 상태와 운영 모드가 복제본이 아닌 원본과 동일합니다."""
        self.assertIs(models.OrderStatus, OrderStatus)
        self.assertIs(models.OperatingMode, OperatingMode)

    def test_order_status_has_exactly_eleven_states(self):
        """명세 7.3절의 11개 KIS 주문 상태만 존재합니다."""
        self.assertEqual(len(models.OrderStatus), 11)

    def test_every_exported_name_exists(self):
        """__all__ 에 적힌 모든 이름이 실제로 존재합니다."""
        for name in models.__all__:
            self.assertTrue(hasattr(models, name), name)


if __name__ == "__main__":
    unittest.main()

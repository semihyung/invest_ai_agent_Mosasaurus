"""명세서 10.1절의 공용 도메인 모델 진입점입니다.

각 모델은 해당 책임을 가진 모듈에 정의되어 있으며, 이 모듈은 여러 모듈이
공유하는 열거형·데이터 클래스를 한 곳에서 가져다 쓸 수 있게 다시 내보냅니다.
정의를 복제하지 않으므로 상태값이 모듈마다 달라질 위험이 없습니다.
"""

from src.corporate_actions import CorporateAction
from src.market_data import DailyCandle, FreshnessStatus, IntradaySnapshot
from src.order_manager import (
    ExitObligation,
    OrderIntent,
    OrderIntentType,
    OrderSide,
    OrderStatus,
    Quote,
)
from src.performance import (
    CashFlowClassificationStatus,
    CashFlowType,
    TWRQualityStatus,
)
from src.risk_manager import DrawdownMode, OperatingMode, RiskAction, RiskFlags
from src.universe import ProductMetadata

__all__ = [
    "CashFlowClassificationStatus",
    "CashFlowType",
    "CorporateAction",
    "DailyCandle",
    "DrawdownMode",
    "ExitObligation",
    "FreshnessStatus",
    "IntradaySnapshot",
    "OperatingMode",
    "OrderIntent",
    "OrderIntentType",
    "OrderSide",
    "OrderStatus",
    "ProductMetadata",
    "Quote",
    "RiskAction",
    "RiskFlags",
    "TWRQualityStatus",
]

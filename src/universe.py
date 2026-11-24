"""상품의 공식 분류 자료와 운영자 승인 출처를 검증합니다."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


_OFFICIAL_SOURCES = {"KRX_ETF_MASTER", "KIS_PRODUCT_MASTER"}
_CLASSIFICATION_FIELDS = {
    "ticker",
    "name",
    "category",
    "region",
    "underlying_index",
    "product_type",
    "replication_method",
    "leverage_multiplier",
    "is_inverse",
    "is_futures_based",
    "classification_source",
    "verified_by",
    "verified_at",
}


@dataclass(frozen=True)
class ProductMetadata:
    """공식 상품 마스터에서 읽은 구조화 ETF 정보를 나타냅니다."""

    ticker: str
    name: str
    region: str
    underlying_index: str
    product_type: str
    replication_method: str
    leverage_multiplier: float
    is_inverse: bool
    is_futures_based: bool
    classification_source: str


def validate_product_metadata(
    metadata: ProductMetadata,
    *,
    expected_ticker: str,
    category: str = "RISK_ASSET",
    allowed_synthetic_parking_tickers: Sequence[str] = (),
) -> None:
    """공식 메타데이터가 현물 ETF 안전 기준을 모두 만족하는지 검사합니다."""
    if not isinstance(metadata, ProductMetadata):
        raise TypeError("공식 상품 조회 결과는 ProductMetadata여야 합니다.")
    if (
        not isinstance(metadata.ticker, str)
        or not metadata.ticker
        or metadata.ticker != expected_ticker
    ):
        raise ValueError("공식 상품 티커가 요청한 티커와 일치하지 않습니다.")
    for field_name in ("name", "underlying_index"):
        value = getattr(metadata, field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"공식 상품 메타데이터의 {field_name} 값이 없습니다.")
    if not isinstance(metadata.region, str) or metadata.region not in {
        "KR",
        "US",
        "GLOBAL",
    }:
        raise ValueError("지원되지 않는 상품 지역입니다.")
    if metadata.product_type != "SPOT_ETF":
        raise ValueError("현물 ETF 이외의 상품은 유니버스에 추가할 수 없습니다.")
    if (
        isinstance(metadata.leverage_multiplier, bool)
        or not isinstance(metadata.leverage_multiplier, (int, float))
        or metadata.leverage_multiplier != 1.0
    ):
        raise ValueError("추적 배수가 1.0이 아닌 상품은 허용되지 않습니다.")
    if metadata.is_inverse is not False or metadata.is_futures_based is not False:
        raise ValueError("인버스·선물형 상품은 허용되지 않습니다.")
    if not isinstance(metadata.replication_method, str) or metadata.replication_method not in {
        "PHYSICAL",
        "SYNTHETIC",
    }:
        raise ValueError("지원되지 않는 ETF 복제 방식입니다.")
    if (
        not isinstance(metadata.classification_source, str)
        or metadata.classification_source not in _OFFICIAL_SOURCES
    ):
        raise ValueError("상품 분류 출처는 KRX 또는 KIS 공식 마스터여야 합니다.")
    if not isinstance(category, str) or category not in {
        "RISK_ASSET",
        "SAFE_PARKING",
    }:
        raise ValueError("지원되지 않는 유니버스 분류입니다.")
    if metadata.replication_method == "SYNTHETIC" and (
        category != "SAFE_PARKING"
        or metadata.ticker not in allowed_synthetic_parking_tickers
    ):
        raise ValueError("합성복제 ETF는 승인된 파킹 종목만 허용됩니다.")


def product_metadata_fingerprint(
    metadata: ProductMetadata,
    *,
    category: str,
    allowed_synthetic_parking_tickers: Sequence[str] = (),
) -> str:
    """상품 분류 필드를 정규화해 승인 이력과 DB 값을 연결하는 지문을 만듭니다."""
    validate_product_metadata(
        metadata,
        expected_ticker=metadata.ticker,
        category=category,
        allowed_synthetic_parking_tickers=allowed_synthetic_parking_tickers,
    )
    payload = {
        "category": category,
        "classification_source": metadata.classification_source,
        "is_futures_based": metadata.is_futures_based,
        "is_inverse": metadata.is_inverse,
        "leverage_multiplier": metadata.leverage_multiplier,
        "name": metadata.name,
        "product_type": metadata.product_type,
        "region": metadata.region,
        "replication_method": metadata.replication_method,
        "ticker": metadata.ticker,
        "underlying_index": metadata.underlying_index,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_aware_timestamp(value: Any) -> str:
    """ISO-8601 승인 시각을 시간대가 포함된 UTC 문자열로 정규화합니다."""
    if not isinstance(value, str):
        raise ValueError("승인 시각은 ISO-8601 문자열이어야 합니다.")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("승인 시각이 올바른 ISO-8601 형식이 아닙니다.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("승인 시각에는 시간대 정보가 필요합니다.")
    return parsed.astimezone(timezone.utc).isoformat()


def _validate_distribution_schedule(schedule: Any) -> None:
    """운영자가 등록한 분배 일정(`ex_date` ISO 날짜 + https 출처 URL)의 형식을 검사한다."""
    if not isinstance(schedule, list):
        raise ValueError("distribution_schedule 은 목록이어야 합니다.")
    for entry in schedule:
        if (not isinstance(entry, dict) or set(entry) != {"ex_date", "source_url"}
                or not isinstance(entry["source_url"], str) or not entry["source_url"].startswith("https://")):
            raise ValueError("분배 일정 항목은 ex_date 와 https source_url 만 가져야 합니다.")
        try:
            parsed = date.fromisoformat(str(entry["ex_date"]))
        except ValueError as exc:
            raise ValueError("분배 일정의 ex_date 는 ISO 날짜여야 합니다.") from exc
        if parsed.isoformat() != str(entry["ex_date"]):
            raise ValueError("분배 일정의 ex_date 는 ISO 날짜여야 합니다.")


def scheduled_ex_dates(assets: Sequence[dict[str, Any]]) -> dict[str, set[str]]:
    """승인 파일 종목별 등록 분배 일정의 ex_date 집합(자동 검증 근거)."""
    return {a["ticker"]: {str(e["ex_date"]) for e in a.get("distribution_schedule", [])} for a in assets}


def load_universe_allowlist(
    path: str | Path,
    *,
    allowed_synthetic_parking_tickers: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """운영자 승인 YAML을 읽고 각 종목의 공식 분류와 출처를 검증합니다."""
    allowlist_path = Path(path).expanduser()
    try:
        payload = yaml.safe_load(allowlist_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"유니버스 승인 파일을 읽을 수 없습니다: {allowlist_path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"유니버스 승인 파일의 YAML 형식이 잘못되었습니다: {allowlist_path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != "3.7.2"
        or not isinstance(payload.get("assets"), list)
        or set(payload) != {"version", "assets"}
    ):
        raise ValueError("승인 파일은 v3.7.2 형식의 version/assets 구조여야 합니다.")

    normalized_assets = []
    seen_tickers = set()
    for item in payload["assets"]:
        if (not isinstance(item, dict)
                or set(item) not in (_CLASSIFICATION_FIELDS, _CLASSIFICATION_FIELDS | {"distribution_schedule"})):
            raise ValueError("승인 파일의 종목 항목 필드가 예상 형식과 다릅니다.")
        _validate_distribution_schedule(item.get("distribution_schedule", []))
        if item["ticker"] in seen_tickers:
            raise ValueError("승인 파일에 중복 티커가 있습니다.")
        seen_tickers.add(item["ticker"])
        metadata = ProductMetadata(
            ticker=item["ticker"],
            name=item["name"],
            region=item["region"],
            underlying_index=item["underlying_index"],
            product_type=item["product_type"],
            replication_method=item["replication_method"],
            leverage_multiplier=item["leverage_multiplier"],
            is_inverse=item["is_inverse"],
            is_futures_based=item["is_futures_based"],
            classification_source=item["classification_source"],
        )
        validate_product_metadata(
            metadata,
            expected_ticker=item["ticker"],
            category=item["category"],
            allowed_synthetic_parking_tickers=allowed_synthetic_parking_tickers,
        )
        if (
            not isinstance(item["verified_by"], str)
            or not item["verified_by"].strip()
            or item["verified_by"] == "UNVERIFIED"
        ):
            raise ValueError("승인 파일에는 승인자 식별자가 필요합니다.")
        if not isinstance(item["ticker"], str) or not item["ticker"].strip():
            raise ValueError("승인 파일의 티커는 비어 있지 않은 문자열이어야 합니다.")
        normalized_assets.append(
            {
                **item,
                "verified_at": _parse_aware_timestamp(item["verified_at"]),
            }
        )
    return normalized_assets


def sync_universe_allowlist(
    connection: sqlite3.Connection,
    assets: Sequence[Mapping[str, Any]],
) -> int:
    """승인된 종목만 추가하고 기존 분류의 변경은 자동으로 덮어쓰지 않습니다."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection은 sqlite3.Connection이어야 합니다.")
    inserted = 0
    with connection:
        for asset in assets:
            existing = connection.execute(
                """
                SELECT name, category, region, underlying_index, product_type,
                       replication_method, leverage_multiplier, is_inverse,
                       is_futures_based, classification_verified,
                       classification_source, verified_by, verified_at
                FROM universe_assets WHERE ticker = ?
                """,
                (asset["ticker"],),
            ).fetchone()
            expected = (
                asset["name"],
                asset["category"],
                asset["region"],
                asset["underlying_index"],
                asset["product_type"],
                asset["replication_method"],
                asset["leverage_multiplier"],
                int(asset["is_inverse"]),
                int(asset["is_futures_based"]),
                1,
                asset["classification_source"],
                f"allowlist:{asset['verified_by']}",
                asset["verified_at"],
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise RuntimeError(
                        f"승인 파일과 DB의 상품 분류가 다릅니다: {asset['ticker']}."
                    )
                continue
            connection.execute(
                """
                INSERT INTO universe_assets (
                    ticker, name, category, region, underlying_index, product_type,
                    replication_method, leverage_multiplier, is_inverse,
                    is_futures_based, classification_verified, classification_source,
                    verified_by, verified_at, status, data_status,
                    blocked_until_next_rotation, added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?,
                          'ACTIVE', 'VALID', 0, ?)
                """,
                (
                    asset["ticker"],
                    asset["name"],
                    asset["category"],
                    asset["region"],
                    asset["underlying_index"],
                    asset["product_type"],
                    asset["replication_method"],
                    asset["leverage_multiplier"],
                    int(asset["is_inverse"]),
                    int(asset["is_futures_based"]),
                    asset["classification_source"],
                    f"allowlist:{asset['verified_by']}",
                    asset["verified_at"],
                    asset["verified_at"],
                ),
            )
            inserted += 1
    return inserted


def has_approved_classification(
    connection: sqlite3.Connection,
    asset: sqlite3.Row,
    *,
    allowlist_by_ticker: Mapping[str, Mapping[str, Any]],
    allowed_synthetic_parking_tickers: Sequence[str] = (),
) -> bool:
    """DB 분류가 현재 Allowlist 또는 승인된 Telegram 확인 이력과 일치하는지 봅니다."""
    if asset["classification_verified"] != 1:
        return False
    metadata = ProductMetadata(
        ticker=asset["ticker"],
        name=asset["name"],
        region=asset["region"],
        underlying_index=asset["underlying_index"],
        product_type=asset["product_type"],
        replication_method=asset["replication_method"],
        leverage_multiplier=asset["leverage_multiplier"],
        is_inverse=bool(asset["is_inverse"]),
        is_futures_based=bool(asset["is_futures_based"]),
        classification_source=asset["classification_source"],
    )
    try:
        validate_product_metadata(
            metadata,
            expected_ticker=asset["ticker"],
            category=asset["category"],
            allowed_synthetic_parking_tickers=allowed_synthetic_parking_tickers,
        )
    except (TypeError, ValueError):
        return False

    verified_by = asset["verified_by"]
    if not isinstance(verified_by, str) or not verified_by:
        return False
    if verified_by.startswith("allowlist:"):
        approved = allowlist_by_ticker.get(asset["ticker"])
        if approved is None:
            return False
        return (
            asset["name"] == approved["name"]
            and asset["category"] == approved["category"]
            and asset["region"] == approved["region"]
            and asset["underlying_index"] == approved["underlying_index"]
            and asset["product_type"] == approved["product_type"]
            and asset["replication_method"] == approved["replication_method"]
            and asset["leverage_multiplier"] == approved["leverage_multiplier"]
            and asset["is_inverse"] == int(approved["is_inverse"])
            and asset["is_futures_based"] == int(approved["is_futures_based"])
            and asset["classification_source"] == approved["classification_source"]
            and verified_by == f"allowlist:{approved['verified_by']}"
            and asset["verified_at"] == approved["verified_at"]
        )
    if verified_by.startswith("telegram:"):
        approval = connection.execute(
            """
            SELECT reason FROM pending_actions
            WHERE action_type = 'UNIVERSE_ADD'
              AND ticker = ?
              AND approval_status = 'APPROVED'
              AND confirmed_at = ?
            LIMIT 1
            """,
            (asset["ticker"], asset["verified_at"]),
        ).fetchone()
        if approval is None:
            return False
        fingerprint = product_metadata_fingerprint(
            metadata,
            category=asset["category"],
            allowed_synthetic_parking_tickers=allowed_synthetic_parking_tickers,
        )
        return f";METADATA_SHA256={fingerprint}" in approval["reason"]
    return False

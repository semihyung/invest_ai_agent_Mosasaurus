"""KIS 주문 REST 어댑터(명세 4.4절 주문 계약). `order_manager` 만 가져다 쓰는 비공개 모듈입니다.

- TR ID 는 환경별 표에서만 고릅니다(`T`→`V` 접두사 치환 같은 자동 변환 금지).
- 지정가(`ORD_DVSN="00"`)만 만들 수 있고, 시장가 코드와 빈 주문단가는 요청 생성 단계에서 거부합니다.
- 실제 HTTP 전송은 주입된 `http_post` 콜백이 합니다. 테스트는 녹화된 응답을 돌려주는 가짜 콜백을
  쓰므로 이 모듈은 어떤 테스트에서도 KIS 서버를 호출하지 않습니다.
- 계좌번호·토큰·앱 키는 로그·예외·repr 에 절대 노출하지 않습니다.
- 주문·정정·취소 POST 는 자동 재시도하지 않습니다(모호한 실패는 `TimeoutError` 로 올려 보내
  호출자가 `PENDING_CONFIRMATION` 으로 처리하게 합니다).
"""

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

# TR ID 표: 접두사 변환 없이 이 표에서만 선택합니다. [VERIFIED: KIS 공식 샘플 2026-11-24]
TR_IDS: Mapping[str, Mapping[str, str]] = {
    "PAPER": {
        "BUY": "VTTC0012U",
        "SELL": "VTTC0011U",
        "RVSECNCL": "VTTC0013U",
        "PSBL_ORDER": "VTTC8908R",
        "BALANCE": "VTTC8434R",
        "DAILY_CCLD": "VTTC0081R",
        "CCNL_NOTICE": "H0STCNI9",
    },
    "REAL": {
        "BUY": "TTTC0012U",
        "SELL": "TTTC0011U",
        "RVSECNCL": "TTTC0013U",
        "PSBL_ORDER": "TTTC8908R",
        "BALANCE": "TTTC8434R",
        "DAILY_CCLD": "TTTC0081R",
        "CCNL_NOTICE": "H0STCNI0",
    },
}
BASE_URLS: Mapping[str, str] = {
    "PAPER": "https://openapivts.koreainvestment.com:29443",
    "REAL": "https://openapi.koreainvestment.com:9443",
}
ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
RVSECNCL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
LIMIT_ORDER_DIVISION = "00"  # 지정가. 시장가("01") 등 다른 값은 만들 수 없습니다.

_TICKER = re.compile(r"\d{6}")
_CANO = re.compile(r"\d{8}")
_PRDT = re.compile(r"\d{2}")

# (url, headers, body, timeout_seconds) -> (http_status, parsed_json_or_None)
HttpPost = Callable[[str, Mapping[str, str], Mapping[str, str], float], tuple[int, Any]]


class OrderRequestError(ValueError):
    """주문 요청 필드가 명세 계약을 어길 때 전송 전에 발생합니다."""


class TokenBucket:
    """프로세스 단일 호출 한도기: 초당 `rate` 회를 넘지 않도록 대기합니다(명세 4.4절)."""

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """호출 간 최소 간격을 계산하고 시계·대기 함수를 주입받습니다(테스트용)."""
        if isinstance(rate_per_second, bool) or not isinstance(rate_per_second, (int, float)):
            raise ValueError("rate_per_second must be numeric.")
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive.")
        self._interval = 1.0 / rate_per_second
        self._clock = clock
        self._sleeper = sleeper
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """다음 호출이 허용될 때까지 대기하고 실제 대기한 시간을 반환합니다."""
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_allowed - now)
            if wait:
                self._sleeper(wait)
            self._next_allowed = max(now, self._next_allowed) + self._interval
            return wait


@dataclass(frozen=True)
class OrderAck:
    """주문·정정·취소 응답에서 추출한 비민감 필드입니다."""

    accepted: bool
    order_number: str | None = None
    org_number: str | None = None
    message_code: str | None = None


def _positive_int(name: str, value: int) -> None:
    """수량·가격이 bool 이 아닌 양의 정수인지 확인합니다."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OrderRequestError(f"{name} must be a positive integer.")


class KisOrderAdapter:
    """환경별 TR ID 표로 KIS 지정가 주문·정정·취소 요청을 만들고 전송합니다."""

    def __init__(
        self,
        environment: str,
        *,
        app_key: str,
        app_secret: str,
        account_number: str,
        token_provider: Callable[[], str],
        http_post: HttpPost,
        rate_limiter: TokenBucket | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        """환경·자격 증명·주입된 전송 함수를 보관합니다(자격 증명은 이 객체 안에서만 사용)."""
        if environment not in TR_IDS:
            raise OrderRequestError("environment must be PAPER or REAL.")
        if not callable(token_provider) or not callable(http_post):
            raise TypeError("token_provider and http_post must be callable.")
        digits = re.sub(r"\D", "", account_number) if isinstance(account_number, str) else ""
        if len(digits) != 10:
            raise OrderRequestError("account number must be 10 digits.")
        for secret in (app_key, app_secret):
            if not isinstance(secret, str) or not secret:
                raise OrderRequestError("app credentials are required.")
        self._environment = environment
        self._app_key = app_key
        self._app_secret = app_secret
        self._cano, self._prdt = digits[:8], digits[8:]
        self._token_provider = token_provider
        self._http_post = http_post
        self._limiter = rate_limiter
        self._timeout = float(timeout_seconds)

    def __repr__(self) -> str:
        """자격 증명·계좌번호 없이 환경만 표시합니다."""
        return f"KisOrderAdapter(environment={self._environment!r})"

    # -- 요청 생성 ---------------------------------------------------------
    def build_order_request(
        self,
        *,
        side: str,
        ticker: str,
        quantity: int,
        price: int,
    ) -> tuple[str, dict[str, str]]:
        """지정가 매수·매도 요청의 (TR ID, body) 를 만듭니다(전송은 하지 않음)."""
        if side not in ("BUY", "SELL"):
            raise OrderRequestError("side must be BUY or SELL.")
        if not isinstance(ticker, str) or _TICKER.fullmatch(ticker) is None:
            raise OrderRequestError("ticker must be a 6-digit code.")
        _positive_int("quantity", quantity)
        # 주문단가를 비우면 상한가 주문이 되므로 양의 정수만 허용합니다.
        _positive_int("price", price)
        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._prdt,
            "PDNO": ticker,
            "ORD_DVSN": LIMIT_ORDER_DIVISION,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "01" if side == "SELL" else "",
            "CNDT_PRIC": "",
        }
        return TR_IDS[self._environment][side], body

    def build_rvsecncl_request(
        self,
        *,
        mode: str,
        org_number: str,
        order_number: str,
        quantity: int,
        price: int,
        all_remaining: bool,
    ) -> tuple[str, dict[str, str]]:
        """정정(`MODIFY`)·취소(`CANCEL`) 요청의 (TR ID, body) 를 만듭니다."""
        if mode not in ("MODIFY", "CANCEL"):
            raise OrderRequestError("mode must be MODIFY or CANCEL.")
        for name, value in (("org_number", org_number), ("order_number", order_number)):
            if not isinstance(value, str) or not value.strip():
                raise OrderRequestError(f"{name} is required.")
        _positive_int("quantity", quantity)
        _positive_int("price", price)
        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._prdt,
            "KRX_FWDG_ORD_ORGNO": org_number,
            "ORGN_ODNO": order_number,
            "ORD_DVSN": LIMIT_ORDER_DIVISION,
            "RVSE_CNCL_DVSN_CD": "01" if mode == "MODIFY" else "02",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
            "QTY_ALL_ORD_YN": "Y" if all_remaining else "N",
            "EXCG_ID_DVSN_CD": "KRX",
            "CNDT_PRIC": "",
        }
        return TR_IDS[self._environment]["RVSECNCL"], body

    # -- 전송 --------------------------------------------------------------
    def _post(self, path: str, tr_id: str, body: Mapping[str, str]) -> OrderAck:
        """한 번만 전송하고 응답을 해석합니다. 모호한 실패는 TimeoutError 로 올립니다."""
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token_provider()}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if self._limiter is not None:
            self._limiter.acquire()
        try:
            status, payload = self._http_post(
                BASE_URLS[self._environment] + path, headers, body, self._timeout
            )
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TimeoutError("KIS order response was not received.") from exc
        return self._interpret(status, payload)

    @staticmethod
    def _interpret(status: int, payload: Any) -> OrderAck:
        """HTTP 상태와 JSON 본문을 접수/명시 거부/모호함으로 분류합니다."""
        if not isinstance(payload, Mapping) or "rt_cd" not in payload:
            # 응답 파싱 불가·필수 필드 누락 = 접수 여부 불명 → PENDING_CONFIRMATION 대상.
            raise TimeoutError("KIS order response could not be interpreted.")
        if status >= 500:
            raise TimeoutError("KIS server error; order state is unknown.")
        message_code = payload.get("msg_cd") if isinstance(payload.get("msg_cd"), str) else None
        if payload.get("rt_cd") == "0":
            output = payload.get("output")
            output = output if isinstance(output, Mapping) else {}
            order_number = output.get("ODNO")  # [필드명 UNVERIFIED]
            if not isinstance(order_number, str) or not order_number:
                raise TimeoutError("Accepted response lacks an order number.")
            org = output.get("KRX_FWDG_ORD_ORGNO")  # [필드명 UNVERIFIED]
            return OrderAck(True, order_number, org if isinstance(org, str) else None, message_code)
        return OrderAck(False, None, None, message_code)

    def submit_limit_order(
        self,
        *,
        ticker: str,
        side: Any,
        quantity: int,
        price: int,
    ) -> Mapping[str, Any]:
        """`LimitOrderTransport` 계약: 지정가 주문 1건을 전송하고 접수/거부 결과를 반환합니다."""
        side_value = getattr(side, "value", side)
        tr_id, body = self.build_order_request(
            side=side_value, ticker=ticker, quantity=quantity, price=price
        )
        ack = self._post(ORDER_PATH, tr_id, body)
        if ack.accepted:
            return {
                "accepted": True,
                "order_number": ack.order_number,
                "org_number": ack.org_number,
            }
        return {"rejected": True, "message_code": ack.message_code}

    def cancel_or_modify(
        self,
        *,
        mode: str,
        org_number: str,
        order_number: str,
        quantity: int,
        price: int,
        all_remaining: bool = True,
    ) -> OrderAck:
        """정정·취소 1건을 전송합니다(자동 재시도 없음)."""
        tr_id, body = self.build_rvsecncl_request(
            mode=mode,
            org_number=org_number,
            order_number=order_number,
            quantity=quantity,
            price=price,
            all_remaining=all_remaining,
        )
        return self._post(RVSECNCL_PATH, tr_id, body)


def urllib_http_post(url: str, headers: Mapping[str, str], body: Mapping[str, str], timeout: float):
    """표준 라이브러리만으로 JSON POST 를 보내는 실제 전송 함수(운영 연결 단계 전용).

    이 함수는 테스트에서 호출하지 않으며, 운영 연결을 활성화하는 별도 승인 전에는
    어디에서도 주입하지 않습니다.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return exc.code, None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise TimeoutError("KIS request failed before a usable response.") from exc

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from src.kis_client import (
    CachedAccessToken,
    KisApiError,
    KisEtfNavWebSocket,
    KisEnvironment,
    KisHttpResponse,
    KisNavTick,
    KisReadOnlyClient,
    KisTokenCache,
    KisTokenIssuer,
    KisTokenRefreshLock,
    KisTokenRefreshService,
    KisTokenRefreshStateStore,
    KisWebSocketApprovalKey,
    KisWebSocketTokenIssuer,
    RefreshTrigger,
    TokenRefreshPolicy,
    should_refresh_access_token,
)


UTC = timezone.utc
POLICY = TokenRefreshPolicy(
    proactive_refresh_min_age_hours=6,
    hard_guard_before_expiry_hours=1,
    min_refresh_cooldown_seconds=65,
)


class KisTokenCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "workspace"
        self.root.mkdir()
        self.cache_path = Path(self.temp_dir.name) / "secrets" / "kis_token.json"
        self.cache = KisTokenCache(self.cache_path, project_root=self.root)

    def test_round_trip_hides_token_from_record_repr(self):
        issued_at = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
        token = CachedAccessToken(
            "FAKE_ACCESS_TOKEN",
            issued_at,
            issued_at + timedelta(hours=24),
        )

        self.cache.save(token)
        loaded = self.cache.load()

        self.assertEqual(loaded, token)
        self.assertNotIn("FAKE_ACCESS_TOKEN", repr(loaded))
        payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertSetEqual(set(payload), {"access_token", "issued_at", "expires_at"})

    def test_returns_none_when_cache_does_not_exist(self):
        self.assertIsNone(self.cache.load())

    def test_default_cache_uses_external_token_directory(self):
        environment = dict(os.environ)
        environment.pop("TOKEN_CACHE_PATH", None)
        with patch.dict(os.environ, environment, clear=True):
            cache = KisTokenCache(project_root=self.root)
        self.assertEqual(
            cache.path,
            Path.home() / ".secrets" / "mosasaurus" / "token-cache" / "token.json",
        )

    def test_rejects_cache_inside_project_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside the project workspace"):
            KisTokenCache(self.root / "data" / "kis_token.json", project_root=self.root)

    def test_rejects_malformed_cache(self):
        self.cache_path.parent.mkdir(parents=True)
        self.cache_path.write_text("{invalid", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            self.cache.load()

    def test_rejects_naive_timestamps(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            CachedAccessToken(
                "FAKE_ACCESS_TOKEN",
                datetime(2026, 11, 21),
                datetime(2026, 11, 22, tzinfo=UTC),
            )


class TokenRefreshPolicyTests(unittest.TestCase):
    def setUp(self):
        self.issued_at = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
        self.token = CachedAccessToken(
            "FAKE_ACCESS_TOKEN",
            self.issued_at,
            self.issued_at + timedelta(hours=24),
        )

    def test_requests_initial_token_when_cache_is_empty(self):
        self.assertTrue(
            should_refresh_access_token(
                None,
                trigger=RefreshTrigger.MORNING_PREP,
                now=self.issued_at,
                policy=POLICY,
                last_request_at=None,
            )
        )

    def test_morning_prep_refreshes_at_six_hour_age(self):
        before_threshold = self.issued_at + timedelta(hours=6) - timedelta(seconds=1)
        at_threshold = self.issued_at + timedelta(hours=6)

        self.assertFalse(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.MORNING_PREP,
                now=before_threshold,
                policy=POLICY,
                last_request_at=None,
            )
        )
        self.assertTrue(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.MORNING_PREP,
                now=at_threshold,
                policy=POLICY,
                last_request_at=None,
            )
        )

    def test_recovery_refreshes_only_inside_one_hour_hard_guard(self):
        before_threshold = self.token.expires_at - timedelta(hours=1, seconds=1)
        at_threshold = self.token.expires_at - timedelta(hours=1)

        self.assertFalse(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.RECOVERY,
                now=before_threshold,
                policy=POLICY,
                last_request_at=None,
            )
        )
        self.assertTrue(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.RECOVERY,
                now=at_threshold,
                policy=POLICY,
                last_request_at=None,
            )
        )

    def test_cooldown_blocks_until_65_seconds_have_elapsed(self):
        now = self.issued_at + timedelta(hours=7)

        self.assertFalse(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.MORNING_PREP,
                now=now,
                policy=POLICY,
                last_request_at=now - timedelta(seconds=64),
            )
        )
        self.assertTrue(
            should_refresh_access_token(
                self.token,
                trigger=RefreshTrigger.MORNING_PREP,
                now=now,
                policy=POLICY,
                last_request_at=now - timedelta(seconds=65),
            )
        )

    def test_rejects_unknown_refresh_trigger(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            should_refresh_access_token(
                self.token,
                trigger="unknown",
                now=self.issued_at,
                policy=POLICY,
                last_request_at=None,
            )


class FakeKisTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers, body, timeout_seconds):
        self.requests.append((method, url, headers, body, timeout_seconds))
        return self.responses.pop(0)


class KisTokenIssuerTests(unittest.TestCase):
    """토큰 발급 요청과 응답 검증을 가짜 전송으로 확인합니다."""

    def test_issues_paper_token_without_logging_credentials(self):
        """모의투자 발급 요청과 토큰 만료 시각을 검증합니다."""
        now = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)
        transport = FakeKisTransport(
            [
                KisHttpResponse(
                    200,
                    json.dumps(
                        {
                            "access_token": "FAKE_NEW_ACCESS_TOKEN",
                            "expires_in": "86400",
                        }
                    ).encode("utf-8"),
                )
            ]
        )
        issuer = KisTokenIssuer(
            "FAKE_APP_KEY",
            "FAKE_APP_SECRET",
            transport=transport,
        )

        token = issuer.issue(now=now)

        self.assertEqual(token.issued_at, now)
        self.assertEqual(token.expires_at, now + timedelta(hours=24))
        method, url, headers, body, _ = transport.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, KisTokenIssuer._TOKEN_URL)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(
            json.loads(body),
            {
                "grant_type": "client_credentials",
                "appkey": "FAKE_APP_KEY",
                "appsecret": "FAKE_APP_SECRET",
            },
        )
        self.assertNotIn("FAKE_NEW_ACCESS_TOKEN", repr(token))

    def test_rejects_bad_token_response_without_echoing_response_body(self):
        """응답 오류 본문이나 민감 문자열을 예외 메시지에 포함하지 않습니다."""
        transport = FakeKisTransport(
            [
                KisHttpResponse(
                    200,
                    b'{"error":"FAKE_SECRET_RESPONSE","expires_in":"invalid"}',
                )
            ]
        )
        issuer = KisTokenIssuer(
            "FAKE_APP_KEY",
            "FAKE_APP_SECRET",
            transport=transport,
        )

        with self.assertRaises(KisApiError) as error:
            issuer.issue(now=datetime(2026, 11, 22, 6, 0, tzinfo=UTC))

        self.assertNotIn("FAKE_SECRET_RESPONSE", str(error.exception))


class KisWebSocketTokenIssuerTests(unittest.TestCase):
    """WebSocket 접속키를 REST 토큰과 분리해 발급하는지 검증합니다."""

    def test_issues_paper_approval_key_without_persisting_it(self):
        """PAPER 승인키 발급 요청과 비밀이 숨겨진 객체 표현을 확인합니다."""
        transport = FakeKisTransport(
            [
                KisHttpResponse(
                    200,
                    b'{"approval_key":"FAKE_WS_APPROVAL_KEY"}',
                )
            ]
        )
        issuer = KisWebSocketTokenIssuer(
            "FAKE_APP_KEY",
            "FAKE_APP_SECRET",
            transport=transport,
        )

        approval_key = issuer.issue()

        self.assertNotIn("FAKE_WS_APPROVAL_KEY", repr(approval_key))
        method, url, headers, body, _ = transport.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, KisWebSocketTokenIssuer._APPROVAL_URL)
        self.assertEqual(
            json.loads(body),
            {
                "grant_type": "client_credentials",
                "appkey": "FAKE_APP_KEY",
                "secretkey": "FAKE_APP_SECRET",
            },
        )
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_rejects_bad_approval_response_without_echoing_body(self):
        """잘못된 응답의 원문을 오류 메시지에 포함하지 않습니다."""
        transport = FakeKisTransport(
            [KisHttpResponse(200, b'{"error":"FAKE_WS_RESPONSE"}')]
        )
        issuer = KisWebSocketTokenIssuer(
            "FAKE_APP_KEY",
            "FAKE_APP_SECRET",
            transport=transport,
        )

        with self.assertRaises(KisApiError) as error:
            issuer.issue()

        self.assertNotIn("FAKE_WS_RESPONSE", str(error.exception))


class FakeKisWebSocketConnection:
    """테스트 중 수신 프레임과 전송 프레임을 메모리에서 기록합니다."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.pongs = []
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        return self.messages.pop(0)

    def pong(self, payload=b""):
        self.pongs.append(payload)

    def close(self):
        self.closed = True


class KisEtfNavWebSocketTests(unittest.TestCase):
    """PAPER NAV 구독, ping 응답과 데이터 검증을 가짜 소켓으로 테스트합니다."""

    def test_subscribes_and_parses_timestamped_nav_tick(self):
        """NAV 프레임의 티커·값·수신 시각을 검증해 변환합니다."""
        received_at = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)
        connection = FakeKisWebSocketConnection(
            [
                '{"header":{"tr_id":"PINGPONG"}}',
                '{"header":{"tr_id":"H0STNAV0"},"body":{"rt_cd":"0"}}',
                "0|H0STNAV0|001|423160^10005.25^0^0^0^0^0^0^0^0^0",
            ]
        )
        connection_calls = []

        def make_connection(url, timeout_seconds):
            connection_calls.append((url, timeout_seconds))
            return connection

        stream = KisEtfNavWebSocket(
            KisWebSocketApprovalKey("FAKE_WS_APPROVAL_KEY"),
            connection_factory=make_connection,
            now=lambda: received_at,
        )
        stream.connect()
        stream.subscribe("423160")

        tick = stream.receive_nav()

        self.assertEqual(
            connection_calls,
            [(KisEtfNavWebSocket._URL, 10)],
        )
        self.assertEqual(
            json.loads(connection.sent[0]),
            {
                "header": {
                    "approval_key": "FAKE_WS_APPROVAL_KEY",
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": "H0STNAV0", "tr_key": "423160"}},
            },
        )
        self.assertEqual(connection.pongs, [b'{"header":{"tr_id":"PINGPONG"}}'])
        self.assertEqual(
            tick,
            KisNavTick(
                ticker="423160",
                nav=10005.25,
                observed_at=received_at,
                received_at=received_at,
            ),
        )

        stream.unsubscribe()
        self.assertEqual(json.loads(connection.sent[1])["header"]["tr_type"], "2")
        stream.close()
        self.assertTrue(connection.closed)

    def test_rejects_unexpected_ticker_and_unsubscribed_receive(self):
        """구독하지 않은 종목이나 비활성 세션을 fail-closed로 거부합니다."""
        connection = FakeKisWebSocketConnection(
            ["0|H0STNAV0|1|459580^10005.25^0^0^0^0^0^0^0^0^0"]
        )
        stream = KisEtfNavWebSocket(
            KisWebSocketApprovalKey("FAKE_WS_APPROVAL_KEY"),
            connection_factory=lambda _url, _timeout: connection,
        )
        stream.connect()
        with self.assertRaisesRegex(KisApiError, "not active"):
            stream.receive_nav()
        stream.subscribe("423160")
        with self.assertRaisesRegex(KisApiError, "does not match"):
            stream.receive_nav()


class KisTokenRefreshServiceTests(unittest.TestCase):
    """토큰 원자적 갱신과 65초 쿨다운을 임시 경로에서 검증합니다."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "workspace"
        self.root.mkdir()
        self.cache_path = Path(self.temp_dir.name) / "secrets" / "token.json"
        self.cache = KisTokenCache(self.cache_path, project_root=self.root)
        self.now = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)
        self.state_store = KisTokenRefreshStateStore(
            self.cache_path.with_name("refresh-state.json"),
            project_root=self.root,
        )

    def make_service(self, responses):
        transport = FakeKisTransport(responses)
        issuer = KisTokenIssuer(
            "FAKE_APP_KEY",
            "FAKE_APP_SECRET",
            transport=transport,
        )
        return (
            KisTokenRefreshService(
                self.cache,
                issuer,
                POLICY,
                state_store=self.state_store,
            ),
            transport,
        )

    def test_refreshes_empty_cache_and_persists_only_cooldown_metadata(self):
        """캐시가 비었을 때 토큰을 발급하고 메타데이터에는 시각만 저장합니다."""
        service, transport = self.make_service(
            [
                KisHttpResponse(
                    200,
                    b'{"access_token":"FAKE_NEW_ACCESS_TOKEN","expires_in":86400}',
                )
            ]
        )

        result = service.refresh_if_needed(
            trigger=RefreshTrigger.RECOVERY,
            now=self.now,
        )

        self.assertEqual(result.action, "refreshed")
        self.assertEqual(self.cache.load().access_token, "FAKE_NEW_ACCESS_TOKEN")
        self.assertEqual(self.state_store.load_last_request_at(), self.now)
        state_payload = json.loads(self.state_store.path.read_text(encoding="utf-8"))
        self.assertEqual(set(state_payload), {"last_request_at"})
        self.assertNotIn("FAKE_NEW_ACCESS_TOKEN", self.state_store.path.read_text())
        self.assertEqual(len(transport.requests), 1)

    def test_failed_issue_keeps_cooldown_and_blocks_early_retry(self):
        """요청 실패 후에도 65초가 지나기 전 재요청을 차단합니다."""
        service, transport = self.make_service(
            [KisHttpResponse(503, b'{"message":"FAKE_RESPONSE"}')]
        )

        with self.assertRaisesRegex(KisApiError, "HTTP 503") as error:
            service.refresh_if_needed(
                trigger=RefreshTrigger.RECOVERY,
                now=self.now,
            )
        self.assertNotIn("FAKE_RESPONSE", str(error.exception))
        with self.assertRaisesRegex(KisApiError, "65-second cooldown"):
            service.refresh_if_needed(
                trigger=RefreshTrigger.RECOVERY,
                now=self.now + timedelta(seconds=64),
            )

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.state_store.load_last_request_at(), self.now)

    def test_refresh_lock_prevents_concurrent_refresh_attempts(self):
        """잠금이 이미 잡혀 있으면 다른 발급 프로세스를 거부합니다."""
        lock_path = self.state_store.path.with_suffix(".lock")

        with KisTokenRefreshLock(lock_path):
            with self.assertRaisesRegex(KisApiError, "already in progress"):
                with KisTokenRefreshLock(lock_path):
                    self.fail("A second process lock should not be acquired.")

    def test_reuses_recent_token_without_requesting_another_token(self):
        """갱신 시점 전의 토큰은 네트워크 요청 없이 재사용합니다."""
        self.cache.save(
            CachedAccessToken(
                "FAKE_ACCESS_TOKEN",
                self.now - timedelta(hours=1),
                self.now + timedelta(hours=23),
            )
        )
        service, transport = self.make_service([])

        result = service.refresh_if_needed(
            trigger=RefreshTrigger.MORNING_PREP,
            now=self.now,
        )

        self.assertEqual(result.action, "reuse")
        self.assertEqual(transport.requests, [])


class KisReadOnlyClientTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "workspace"
        self.root.mkdir()
        self.cache = KisTokenCache(
            Path(self.temp_dir.name) / "secrets" / "token.json",
            project_root=self.root,
        )
        self.now = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)
        self.cache.save(
            CachedAccessToken(
                "FAKE_ACCESS_TOKEN",
                self.now - timedelta(hours=1),
                self.now + timedelta(hours=23),
            )
        )

    def make_client(self, responses):
        transport = FakeKisTransport(responses)
        client = KisReadOnlyClient(
            "FAKE_APP_KEY",
            "FAKE_APP_SECRET",
            self.cache,
            transport=transport,
            now=lambda: self.now,
        )
        return client, transport

    @staticmethod
    def response(payload, status_code=200, headers=None):
        return KisHttpResponse(
            status_code,
            json.dumps(payload).encode("utf-8"),
            headers or {},
        )

    def test_etf_quote_uses_paper_read_only_endpoint(self):
        client, transport = self.make_client(
            [self.response({"rt_cd": "0", "output": {"stck_prpr": "10000"}})]
        )

        output = client.fetch_etf_quote("423160")

        self.assertEqual(output["stck_prpr"], "10000")
        method, url, headers, body, _ = transport.requests[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith(KisReadOnlyClient._BASE_URLS[KisEnvironment.PAPER]))
        self.assertIn("/uapi/etfetn/v1/quotations/inquire-price?", url)
        self.assertEqual(headers["tr_id"], "FHPST02400000")
        self.assertIsNone(body)

    def test_daily_history_is_split_into_safe_date_windows(self):
        client, transport = self.make_client(
            [
                self.response({"rt_cd": "0", "output2": []}),
                self.response({"rt_cd": "0", "output2": []}),
            ]
        )

        rows = client.fetch_daily_candles(
            "423160",
            date(2026, 1, 1),
            date(2026, 4, 10),
            adjusted=True,
        )

        self.assertEqual(rows, [])
        self.assertEqual(len(transport.requests), 2)
        self.assertIn("FID_ORG_ADJ_PRC=0", transport.requests[0][1])
        self.assertIn("FID_ORG_ADJ_PRC=0", transport.requests[1][1])

    def test_account_balance_follows_all_pages_and_splits_account_number(self):
        """잔고 조회가 8-2 계좌 분리와 KIS 연속조회 커서를 적용합니다."""
        client, transport = self.make_client(
            [
                self.response(
                    {
                        "rt_cd": "0",
                        "output1": [{"pdno": "423160"}],
                        "output2": {"dnca_tot_amt": "1000000"},
                        "ctx_area_fk100": "FAKE_FK",
                        "ctx_area_nk100": "FAKE_NK",
                    },
                    headers={"tr_cont": "M"},
                ),
                self.response(
                    {
                        "rt_cd": "0",
                        "output1": [{"pdno": "459580"}],
                        "output2": {"dnca_tot_amt": "1000000"},
                    },
                    headers={"tr_cont": "D"},
                ),
            ]
        )

        positions, summary = client.fetch_account_balance("1234567801")

        self.assertEqual([row["pdno"] for row in positions], ["423160", "459580"])
        self.assertEqual(summary["dnca_tot_amt"], "1000000")
        self.assertEqual(transport.requests[0][2]["tr_id"], "VTTC8434R")
        self.assertEqual(transport.requests[0][2]["tr_cont"], "")
        self.assertEqual(transport.requests[1][2]["tr_cont"], "N")
        first_query = parse_qs(urlsplit(transport.requests[0][1]).query)
        next_query = parse_qs(urlsplit(transport.requests[1][1]).query)
        self.assertEqual(first_query["CANO"], ["12345678"])
        self.assertEqual(first_query["ACNT_PRDT_CD"], ["01"])
        self.assertEqual(next_query["CTX_AREA_FK100"], ["FAKE_FK"])
        self.assertEqual(next_query["CTX_AREA_NK100"], ["FAKE_NK"])

    def test_daily_order_executions_uses_paper_read_only_endpoint(self):
        """주문·체결 조회는 PAPER 전용 TR과 단일 거래일을 사용합니다."""
        client, transport = self.make_client(
            [
                self.response(
                    {
                        "rt_cd": "0",
                        "output1": [{"odno": "FAKE_ORDER", "rmn_qty": "0"}],
                        "output2": {"tot_ord_qty": "1"},
                    }
                )
            ]
        )

        orders, summary = client.fetch_daily_order_executions(
            "1234567801",
            date(2026, 11, 20),
        )

        self.assertEqual(orders[0]["odno"], "FAKE_ORDER")
        self.assertEqual(summary["tot_ord_qty"], "1")
        self.assertEqual(transport.requests[0][2]["tr_id"], "VTTC0081R")
        query = parse_qs(urlsplit(transport.requests[0][1]).query)
        self.assertEqual(query["CANO"], ["12345678"])
        self.assertEqual(query["ACNT_PRDT_CD"], ["01"])
        self.assertEqual(query["INQR_STRT_DT"], ["20261120"])
        self.assertEqual(query["INQR_END_DT"], ["20261120"])
        self.assertEqual(query["CCLD_DVSN"], ["00"])
        self.assertIsNone(transport.requests[0][3])

    def test_account_queries_reject_invalid_account_number_before_network(self):
        """잘못된 계좌번호는 어떤 네트워크 요청보다 먼저 거부합니다."""
        client, transport = self.make_client([])

        with self.assertRaisesRegex(ValueError, "exactly ten digits"):
            client.fetch_account_balance("1234")

        self.assertEqual(transport.requests, [])

    def test_pagination_missing_cursor_fails_closed(self):
        """추가 페이지 표시는 있지만 커서가 없으면 부분 결과를 거부합니다."""
        client, _ = self.make_client(
            [
                self.response(
                    {
                        "rt_cd": "0",
                        "output1": [],
                        "output2": {},
                    },
                    headers={"tr_cont": "M"},
                )
            ]
        )

        with self.assertRaisesRegex(KisApiError, "missing its cursor"):
            client.fetch_account_balance("1234567801")

    def test_kis_error_is_explicit_and_does_not_expose_message_text(self):
        client, _ = self.make_client(
            [
                self.response(
                    {"rt_cd": "1", "msg_cd": "EGW00001", "msg1": "sensitive response"}
                )
            ]
        )

        with self.assertRaisesRegex(KisApiError, "EGW00001") as error:
            client.fetch_etf_quote("423160")
        self.assertNotIn("sensitive response", str(error.exception))
        self.assertNotIn("FAKE_ACCESS_TOKEN", str(error.exception))

    def test_expired_token_stops_before_transport(self):
        self.cache.save(
            CachedAccessToken(
                "FAKE_ACCESS_TOKEN",
                self.now - timedelta(hours=24),
                self.now,
            )
        )
        client, transport = self.make_client([])

        with self.assertRaisesRegex(KisApiError, "unavailable or expired"):
            client.fetch_etf_quote("423160")
        self.assertEqual(transport.requests, [])

    def test_real_environment_is_disabled_until_token_cache_is_bound(self):
        with self.assertRaisesRegex(ValueError, "REAL KIS access is disabled"):
            KisReadOnlyClient(
                "FAKE_APP_KEY",
                "FAKE_APP_SECRET",
                self.cache,
                environment=KisEnvironment.REAL,
            )

    def test_holiday_output_object_is_supported(self):
        client, _ = self.make_client(
            [
                self.response(
                    {
                        "rt_cd": "0",
                        "output": {"bass_dt": "20261123", "opnd_yn": "Y"},
                    }
                )
            ]
        )

        self.assertTrue(client.is_open_day(date(2026, 11, 23)))

    def test_holiday_result_is_reused_for_the_same_date(self):
        client, transport = self.make_client(
            [
                self.response(
                    {
                        "rt_cd": "0",
                        "output": [{"bass_dt": "20261123", "opnd_yn": "N"}],
                    }
                )
            ]
        )

        self.assertFalse(client.is_open_day(date(2026, 11, 23)))
        self.assertFalse(client.is_open_day(date(2026, 11, 23)))
        self.assertEqual(len(transport.requests), 1)


if __name__ == "__main__":
    unittest.main()

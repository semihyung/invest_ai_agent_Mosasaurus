# 모사사우르스 (Mosasaurus)

라즈베리파이 5 기반 자동 투자 에이전트 프로젝트입니다. 개발의 유일한 설계 기준은 [개발 명세 v3.7.3](mosasaurus_spec_v3.7.3.md)이며, 명세에 정한 안전 장치와 단계별 진행 순서를 우선합니다.

> 이 프로젝트는 아직 개발 중이며, 수익이나 원금 보전을 보장하지 않습니다. 현재 버전은 실계좌 주문이나 자동 운용에 사용할 수 없습니다.

## 현재 개발 상태

- SQLite WAL 데이터베이스와 Phase 1 핵심 20개 및 확장용 3개 테이블의 초기화 코드를 구현했습니다.
- v3.7.2 상품 분류 마이그레이션을 추가했습니다. 기존 종목은 모두 미검증으로 남고, 승인된 공식 메타데이터가 없으면 프리뷰와 신규 후보에서 제외됩니다.
- Quant 엔진의 복리 초과수익률 점수·동률 처리·6단계 후보 필터와 수정주가 기준 120일 이동평균 검사를 구현했습니다.
- 외부 토큰 캐시, KIS 모의투자 토큰 발급·갱신기, YAML 및 환경 변수 설정 로더, 조회 전용 REST 잔고·주문/체결 조회와 ETF NAV WebSocket 클라이언트를 구현했습니다. KIS 실계좌 연결 검증과 주문 API는 아직 완료되지 않았습니다.
- 시장 데이터 스냅샷의 TTL, 호가 스프레드, iNAV 괴리율과 검증된 기업행동 조정 규칙을 구현했습니다.
- KIS ETF 현재가·호가·일봉·휴장일 응답 변환과 PAPER NAV WebSocket 클라이언트·모의 응답 테스트를 추가했습니다. 실제 KIS 연결 및 API 응답 검증은 아직 남아 있습니다.
- DataQualityGate가 예상 거래일, 시세 신선도, 계좌·주문·현금흐름·캘린더 및 기업행위 검증을 확인합니다. 아직 실제 주문 파이프라인과 연결되지는 않았습니다.
- 토요일 주간 프리뷰 산출, 집중도 제한, 45/30/20 슬롯 배분, 첫 개장일 차순위 승계·보유 종목 생존 처리와 Smart DCA 계산을 구현했습니다.
- 수정주가 기준 단조 증가 PeakPrice·트레일링 스탑, 단위화 TWR/HWM, 낙폭별 매수 제한과 중앙 운영 모드별 주문 권한 판정을 구현했습니다.
- 멱등성·확인 대기 복구·지정가 전용 주문 상태 머신, 매도 의무 기록, 잔고·주문 대조 및 불일치 시 SAFE_HALT 처리를 구현했습니다.
- APScheduler의 KST 예약 작업·미스파이어 잠금, 부팅 복구 게이트, Telegram 허용 채팅·단일 polling 잠금·DB 기반 2단계 확인을 구현했습니다. 주간 프리뷰 콜백은 테스트됐지만 `main`에 연결되지 않았으며, 다른 운영 작업 콜백·실제 헬스 워치독과 기기 검증은 남아 있습니다.
- 운영자 승인 종목은 `config/universe_allowlist.yaml`로 반영됩니다. 현재 목록은 비어 있어 시작 시 승인된 종목을 자동으로 추가하지 않습니다. Telegram `/add`는 공식 상품 메타데이터 조회 콜백이 연결되지 않으면 안전하게 차단됩니다.
- Phase 1 백테스트 입력과 핵심 CAGR/MDD/turnover 계산 및 25개 장애 시나리오 검증을 구현했습니다.
- Docker Compose, GitHub Actions 배포, 장중 배포 차단과 SQLite 백업·복구 스크립트를 구성했습니다.
- 전략 전체 실행 파이프라인, KIS 주문·체결 연동, 실시간 NAV 구독, 실제 Walk-Forward/Out-of-Sample 백테스트 및 운영 배포 검증은 아직 완료되지 않았습니다.
- Phase 2(OpenDART, 뉴스, LLM 분석)는 승인 전까지 구현하거나 활성화하지 않습니다.

## 프로젝트 구조

```text
mosasaurus_spec_v3.7.3.md      # 유일한 설계 기준
config/rules.yaml              # 전략 및 안전 정책 설정
migrations/001_init_phase1.sql # Phase 1 기본 데이터베이스 스키마
migrations/002_verified_universe_classification.sql # 검증된 상품 분류 필드
config/universe_allowlist.yaml # 운영자 사전 승인 ETF 목록
src/db.py                      # SQLite WAL 초기화 및 검증
src/config.py                  # YAML 설정과 런타임 환경 변수 로더
src/kis_client.py              # 토큰 캐시 및 갱신 정책
src/market_data.py             # 호가·iNAV 신선도와 일봉 품질 검증
src/corporate_actions.py       # 검증된 기업행동 조정 처리
src/quant_engine.py            # 핵심 모멘텀 계산과 데이터 검사
src/universe.py                # 공식 상품 메타데이터와 승인 목록 검증
src/boot_recovery.py           # 부팅 DB·토큰·계좌 복구 게이트
src/main.py                    # 콜백 의존성 주입·Fail-Closed 런타임 조립
src/scheduler.py               # KST 예약 작업·미스파이어 잠금
src/scheduler_jobs.py          # 주간 프리뷰 콜백 (런타임 미연결)
src/telegram_bot.py            # Phase 1 Telegram 접근·확인 제어
src/health_monitor.py          # DB·heartbeat·워치독 헬스 게이트
src/models.py                  # 공용 도메인 모델 재노출
tests/                         # unit · integration · replay · fault_injection 자동화 테스트
backtest/                      # 데이터 로더·백테스트·워크포워드 검증
```

## 개발 환경 및 테스트

명세 기준 Python 3.12 이상을 사용합니다. 프로젝트 루트에서 전체 테스트를 실행하려면:

```bash
python -m unittest discover -s tests -t . -v
# 또는 (pytest 설치 시, 명세 3.4절 CI 방식)
python -m pytest tests/unit tests/integration tests/replay tests/fault_injection
```

테스트는 가짜 데이터와 임시 파일을 사용하며 KIS에 연결하거나 주문하지 않습니다.
의존성은 `python -m pip install -r requirements.txt`로 설치합니다. 호스트 토큰 갱신기는 외부 `.env`를 직접 읽지 않으므로 `systemd EnvironmentFile=` 등으로 `KIS_APP_KEY`, `KIS_APP_SECRET` 환경 변수를 주입한 뒤 `--trigger morning_prep` 또는 `--trigger recovery`로 실행해야 합니다. systemd 타이머 자체는 아직 구성되지 않았습니다.

## 비밀 정보 및 안전

- 실제 API 키, 계좌번호, Telegram 토큰 및 OAuth 토큰 캐시를 코드·테스트·Git에 넣지 마세요. 비밀값을 채팅으로 보내지 마세요.
- 실제 비밀 정보는 프로젝트 폴더 밖의 `~/.secrets/mosasaurus/`에 보관합니다. `.env.example`에는 변수명만 있고 실제 값은 넣지 않습니다.
- KIS 토큰 캐시는 프로젝트 밖의 `~/.secrets/mosasaurus/token-cache/token.json`에 보관합니다. 컨테이너에는 토큰 전용 디렉터리만 `/run/secrets:ro`로 마운트합니다.
- 시장가 주문은 금지됩니다. 실계좌 주문은 명세의 최종 검증을 모두 통과하고 사용자가 별도로 승인하기 전에는 수행하지 않습니다.
- Raspberry Pi 하드웨어는 배포·운영 검증 단계에서 필요합니다. 현재 단계에서는 모의 데이터 기반 개발과 테스트를 진행할 수 있습니다.

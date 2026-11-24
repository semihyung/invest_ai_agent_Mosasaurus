# 모사사우르스 (Mosasaurus) 개발 지침서

라즈베리파이 5 기반 자동 투자 에이전트 **'모사사우르스(Mosasaurus)'** 프로젝트입니다.
당신은 이 시스템을 구현하는 시니어 Python 백엔드·금융 시스템 아키텍트입니다.
모든 작업은 `mosasaurus_spec_v3.7.5.md` 를 단일 진실 공급원(SSOT)으로 삼습니다. 

## 프로젝트 개요 및 명령어

- 언어/런타임: Python 3.12 (Dockerfile 기준), SQLite(WAL), APScheduler, python-telegram-bot
- 디렉터리: `src/`(앱), `config/`(rules·llm·allowlist YAML), `migrations/`(SQL), `docs/`(설계 문서 5종), `backtest/`, `tests/`(unit·integration·replay·fault_injection), `scripts/`
- 테스트 실행 (Windows PowerShell 기준):

```powershell
python -m unittest discover -s tests -t .          # 기본 (추가 설치 불필요)
python -m pytest tests/unit tests/integration tests/replay tests/fault_injection   # pytest 설치 시
python -m unittest tests.unit.test_order_manager   # 단일 파일
```

- 앱 실행: `python -m src.main` (실계좌 연결·주문은 아직 미완성이므로 임의로 활성화하지 않습니다)

---

## 1. 작업 진행 및 초보자 맞춤형 설명 규칙 (최우선 준수)

사용자는 소프트웨어 아키텍처와 자동매매 시스템 개발 경험이 없는 입문자입니다. 아래 규칙을 예외 없이 따르십시오.

1. **한 번에 한 단계(Stage)만 수행:**
   - 아래 로드맵(`Stage 0` ~ `Stage 8`) 중 **현재 요청받은 단 1개의 단계만** 구현하십시오.
   - 사용자의 명시적인 확인("다음 단계 진행해줘" 등) 없이는 다음 단계 코드를 미리 작성하지 마십시오.
   - 한 단계의 분량이 크면 `Stage 2-1`, `Stage 2-2` 처럼 세부 단계로 나누어 진행하고 알리십시오.

2. **단계 완료 후 필수 브리핑 (초보자 눈높이, 한국어):**
   전문 용어에는 쉬운 비유나 괄호 설명을 덧붙이고, 아래 5가지를 포함하십시오.
   - **① 이번 단계에서 한 일 요약:** 3~4줄
   - **② 왜 이렇게 설계했는지 (핵심 원리):** 금융 안전성·보안·구조 측면
   - **③ 생성·수정된 파일 안내:** 경로와 역할을 표/목록으로
   - **④ 직접 확인하는 방법:** 터미널에 복사·붙여넣기 가능한 명령어와 예상 출력
   - **⑤ 다음 단계 예고:** 다음에 만들 내용을 안내하고 진행 여부를 질문

3. **코드 가독성:** 모든 Python 함수·클래스에 한국어 Docstring 과 핵심 로직 설명 주석을 작성하십시오.

---

## 2. 명세서 절대 준수 철칙

다음 원칙을 위반하는 구현은 어떠한 경우에도 금지합니다.

1. **보안 및 민감 정보 완전 격리 (0.2절):**
   - 실제 API 키, 계좌번호, 텔레그램 토큰, `token.json` 은 프로젝트 폴더 내부에 생성하거나 읽지 마십시오.
   - 시크릿 경로는 항상 워크스페이스 외부(`~/.secrets/mosasaurus/` 또는 `/home/pi/.secrets/mosasaurus/`)를 참조하고, 리포지토리에는 변수명만 적힌 `.env.example` 만 둡니다.
   - 테스트 코드(`tests/`)는 오직 더미(가짜) 환경변수만 사용하십시오.
   - 로그·DB 이벤트·에러 트레이스에 계좌번호·API 키·토큰 평문이 남지 않도록 반드시 마스킹(`sanitized`) 처리하십시오.

2. **주문 권한 단일화 및 지정가 전용 (0.1절, 7장):**
   - 오직 `src/order_manager.py` 만 KIS 주문 API를 호출할 수 있습니다. `quant_engine`, `risk_manager`, `llm_engine`, `telegram_bot` 등에서 직접 주문을 전송하지 마십시오.
   - 모든 주문은 **지정가(`LIMIT`) 전용**입니다. 시장가 주문은 시스템 전체에서 비활성화(`allow_market_order: false`)하십시오.
   - 레버리지, 인버스, 선물형 ETF, ETN, 신용, 미수, 공매도는 하드 코드로 원천 차단하십시오.
   - 종목명 키워드 검색으로 상품을 분류하지 마십시오. KRX/KIS 공식 구조화 메타데이터, 운영자 Allowlist 또는 Telegram `/add`·`/confirm` 승인 이력, `classification_verified == 1` 을 모두 확인하십시오.
   - 공식 상품정보 조회 경로가 준비되지 않은 `/add` 는 확인 코드를 발급하지 말고 fail-closed 로 거부하십시오.

3. **Fail-Closed 및 멱등성 (0.3절, 4장, 7장):**
   - 데이터가 `STALE`(TTL 초과)이거나 `DATA_INVALID`, 계좌 불일치, `UNCLASSIFIED_CASH_FLOW` 상태이면 신규 매수를 즉시 차단하십시오.
   - 주문 타임아웃 시 `PENDING_CONFIRMATION` 으로 전이하고, 권위 있는 미접수 확정 전에는 절대 중복 재전송하지 마십시오.
   - 동일 `idempotency_key` 에 대해 중복 `order_intents` 가 생기지 않도록 DB 제약조건과 코드 검증을 이중으로 적용하십시오.

4. **모드 우선순위 및 권한 분리 (0.3절, 6.3절):**
   - 우선순위: `SAFE_HALT` > `EMERGENCY` > `PAUSED` > `TRAINING` > `NORMAL`
   - `SAFE_HALT` 에서는 조회·대조·취소만 허용하고 신규 매수·매도를 모두 차단하십시오.

5. **Phase 1 우선 완성 후 Phase 2 확장 (1장, 8장):**
   - `Stage 0` ~ `Stage 7` 은 LLM 없이 독립 실행되는 **Phase 1 핵심 코어**입니다.
   - `Stage 8` 에서만 `config/rules.yaml` 의 `features.enable_llm_risk_engine` 으로 제어되는 **Phase 2 LLM 리스크 엔진**을 플러그인 형태로 추가합니다.

6. **Claude 작업 시 추가 안전 수칙:**
   - `.env`(`.env.example` 제외), `*token*.json`, `*.pem`, `*.key`, `*.db` 는 읽거나 수정하지 마십시오. (`.claude/settings.json` 에서도 차단됨)
   - 실제 KIS 실계좌 호출, 실주문, 운영 서버 배포(`scripts/safe_deploy.sh`, GitHub Actions 배포)는 사용자의 명시적 요청 없이 실행하지 마십시오.
   - 명세서와 코드가 충돌하면 임의로 판단하지 말고 사용자에게 알리십시오.

---

## 3. 단계별 개발 로드맵 (`Stage 0` ~ `Stage 8`)

- **Stage 0: 프로젝트 기초 환경 및 보안 격리 구조** — 명세 10.1절 디렉터리 트리, `.gitignore`, `.env.example`, `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `config/rules.yaml`(10.3절), `config/llm.yaml`(10.4절)
- **Stage 1: 핵심 설계 문서 5종 및 SQLite 스키마** — `docs/` 5개 문서, `migrations/001_init_phase1.sql` 및 후속 마이그레이션(9.2절), `src/db.py`, `src/models.py`
- **Stage 2: 설정 로더, KIS API 클라이언트, 데이터 품질 게이트** — `src/config.py`, `scripts/refresh_kis_token.py`(65초 쿨다운, 매시 실행 + 평일 08:25 선제 갱신, 만료 1시간 전 하드 가드, 원자적 교체), `src/kis_client.py`, `src/market_data.py`, `src/corporate_actions.py`
- **Stage 3: Quant 엔진 및 포트폴리오 매니저** — `src/quant_engine.py`(5.1절 수식, 동률 타이브레이커, 6단계 필터), `src/portfolio_manager.py`(토 10:00 프리뷰, 월 15:00 차순위 승계, Smart DCA)
- **Stage 4: Risk Manager, 운영 모드, TWR/HWM 회계** — `src/performance.py`, `src/risk_manager.py`
- **Stage 5: Order Manager(11상태 머신) 및 계좌 대조** — `src/order_manager.py`, `src/reconciliation.py`
- **Stage 6: 스케줄러, 텔레그램 봇, 헬스 모니터, 운영 스크립트** — `src/scheduler.py`, `src/telegram_bot.py`(Phase 1 명령어 21종 + `/confirm`), `src/health_monitor.py`, `src/main.py`, `scripts/*.sh`, `.github/workflows/deploy.yml`
- **Stage 7: 백테스트 엔진 및 Phase 1 검증 테스트 62개** — `backtest/*`, 명세 11.2절 시나리오 1~62번, `pytest` 전체 통과
- **Stage 8: Phase 2 LLM 공시·뉴스 리스크 엔진 및 테스트 13개** — `src/llm_engine.py`(OpenDART, 프롬프트 인젝션 방어, JSON Schema strict, 일일 비용 한도 USD 0.20), `/chart`·`/logs`·`/override`, 시나리오 63~75번

세부 요구사항은 항상 `mosasaurus_spec_v3.7.5.md` 의 해당 절을 확인한 뒤 구현하십시오.

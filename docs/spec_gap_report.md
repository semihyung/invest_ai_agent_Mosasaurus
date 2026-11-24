# 명세 v3.7.5 ↔ 코드 격차 보고서 (S0.1–S8 완료 후 v3.7.5 정렬 반영)

작성 기준: 자동 테스트 483개 통과(Phase 1 마무리 3차 후, 이전 422개)(`python -m unittest discover -s tests -t .`). **이 보고서는 "구현됨"과 "부품만 있고 아직 연결되지 않음"을 구분합니다.**
실계좌·실서버 호출, 운영 서버 배포, 쉘 스크립트의 실제 실행(`sh -n` 문법 검사만 수행)은 하지 않았습니다.

## 1. 단계별 변경 요약

| 단계 | 내용 | 주요 파일 |
| --- | --- | --- |
| S0.1 | 명세 v3.7.4 패치(승인 명령 3종, 예수금 필드, 취소 후 지연 체결, 슬롯 재정렬 등) | `mosasaurus_spec_v3.7.4.md` |
| S1 | 격차 감사 | 이 문서 |
| S2 | 순수 규칙: 진리표·전이표·미접수 확정·슬롯·낙폭 히스테리시스·HWM 동결/epoch/리베이스·변동성 하한·설정 정합성·예수금 대사 | `order_policy.py`, `order_manager.py`, `portfolio_manager.py`, `risk_manager.py`, `performance.py`, `quant_engine.py`, `config.py`, `cash_recon.py`, `config/rules.yaml` |
| S3 | 마이그레이션 003: 26개 테이블·뷰·트리거·partial index | `migrations/003_spec_v374_schema.sql`, `db.py` |
| S4 | `safety_flags`(SAFE_HALT 원인), 부팅 복구 Step 0–6, 호스트 상태 파일(UPS·NTP) | `safety_flags.py`, `boot_recovery.py`, `reconciliation.py`, `scheduler.py` |
| S5 | KIS 주문 어댑터(PAPER/REAL TR 표, 재생 테스트), ETF 호가표, 비상 재가격, 체결 증분, 지연 체결 보정, 의무 전이 | `_kis_order_adapter.py`, `order_manager.py` |
| S6 | Telegram 확인 코드 보안·사용자 ID·속도 제한, 승인·해제·분류·검증 작업 | `telegram_bot.py`, `operator_actions.py` |
| S7 | 배포 금지 시간·서명 검증 래퍼·10단계 배포·systemd·네트워크 감시·컨테이너 하드닝 | `deployment.py`, `net_watchdog.py`, `scripts/*`, `docker-compose.yml`, `Dockerfile`, `deploy.yml` |
| S8 | 운영 코드를 호출하는 백테스트 엔진·Walk-Forward·승인 산출물 | `backtest/engine.py`, `walk_forward.py`, `report.py` |

## 2. 사용자 확인이 필요한 결정(명세와 코드 사이에서 제가 정한 것)

1. `pending_actions.action_type` 이름을 기존 코드에 맞춰 `UNIVERSE_ADD`/`UNIVERSE_REMOVE` 로 확정하고 명세를 고쳤습니다.
2. `SAFE_PARKING` 의 `asset_class` 는 `MONEY_MARKET` 으로 허용했습니다(명세 0.5절에 반영). `RISK_ASSET` 은 `EQUITY` 만 DB `CHECK` 로 강제합니다.
3. `/resume`, `/training on|off` 를 명세 6.4절대로 2단계 확인으로 바꿨습니다. `run_telegram_polling` 은 새 명령 6종의 콜백을 요구하므로 **운영 연결 단계에서 콜백 주입이 필요**합니다.
4. 기존 `boot_recovery` 의 `SAFE_HALT_ALREADY_LATCHED`(영구 정지)를 원인별 자동/수동 해제로 대체했습니다. 기존 테스트 일부를 이 동작에 맞게 다시 썼습니다.
5. `CREATED → REJECTED`, `PARTIALLY_FILLED → CANCELED`, `CONFIRMED_NOT_ACCEPTED → SUBMITTING` 전이는 명세 7.3절 표에 없어 제거했습니다.
6. 배포 금지 시간은 `08:50–15:25` 에서 `08:20–15:45`(+토·일 구간, 완료 신호)로 넓혔고, 기존 `safe_deploy.sh` 는 10단계 절차로 교체했습니다.
7. (v3.7.5) 비상 재가격 함수(`next_emergency_price`·`calculate_emergency_reprice`·`tick_down`)는 **삭제**했습니다. 위험 축소 매도 가격은 `calculate_limit_price(..., reduce_only=True)`(Bid1 1회)입니다.

## 3. 아직 연결(배선)되지 않은 부분 — 구현 착수 전 반드시 알아야 할 것

| 항목 | 상태 |
| --- | --- |
| 15:00 `daily_pipeline` 실주문 흐름(보유 유지 판정→진입 게이트→매도 선행→매수) | `scheduler_jobs.py` 에는 **토요일 프리뷰 핸들러만** 있습니다. 명세 5.3 Step B 전체는 부품(`select_final_slots`, `order_policy`, 어댑터, 체결 기록)만 있고 오케스트레이션이 없습니다 |
| `post_close_reconcile`(15:40), `morning_prep`(기업행위 감지), `intraday_guard`, `open_checkpoint`, `pending_confirmation_poller` | 잡 핸들러 미구현. 규칙은 순수 함수·문서로만 존재 |
| 기업행위 **감지**(소급 조정·`ex_date` 비교로 `corporate_actions` 후보 생성) | 미구현. 검증(`/ca_confirm`)과 재기준화만 구현 |
| 예수금 대사 **실행**(`cash_recon` 함수를 잔고 조회와 연결) | 미구현. 순수 함수만 있음. 필드명 `prvs_rcdl_excc_amt` 는 `[UNVERIFIED]` |
| 티커별 상호배제 락·`SELL_ALREADY_ACTIVE` 정책 | 미구현(같은 의도의 활성 attempt 중복은 DB 가 막지만 서로 다른 의도의 동시 매도는 코드가 막지 않음) |
| `idempotency_key` 의 `cycle_id` 포맷 헬퍼 | 미구현(형식은 명세·문서에만 존재) |
| (v3.7.5) 비상 재가격 루프 | 명세·코드에서 삭제됨(해당 없음) |
| 호출 우선순위 큐·REST 폴링 주기 자동 연장 | 토큰 버킷(`TokenBucket`)만 구현 |
| `RealOrderGateContext` 가 `real_gate_inputs` 를 쓰도록 연결 | 계산 함수는 있으나 호출부 없음(게이트는 여전히 호출자가 넘긴 불리언을 신뢰) |
| 실제 HTTP 전송·WebSocket·휴장일 API | `urllib_http_post` 는 작성만 했고 어디에서도 주입하지 않음. 실서버 호출 없음 |
| 거래일 15:10 이후 스탑 → `exit_obligations` 기록 | 규칙 문서화만 |
| Telegram 명령 콜백(`/clear_halt` 등)과 `operator_actions` 연결, `main.py` 의 `allowed_user_ids` 주입 | 미연결 |
| `safe_deploy.sh`·`mosasaurus-deploy`·systemd·`approve_*.sh` 의 실제 실행 | `sh -n` 문법 검사와 승인 스크립트의 임시 디렉터리 실행만 확인. **Raspberry Pi 5 에서 검증 필요** |
| `[UNVERIFIED]` 25곳(주문 응답 필드명, WebSocket URL, 휴장일 PAPER 지원, 호출 한도, ETF 호가표 등) | 포털 확인 전까지 fail-closed 기본값 |
| Phase 2(LLM, 시나리오 63–75) | CLAUDE.md Stage 8 범위로, 이번 연속 진행에 포함하지 않음 |

## 4. 11.2절 시나리오 커버리지 (정직한 표기)

`✅` 자동 테스트로 직접 검증, `🟡` 구성 요소만 검증(통합 배선 없음), `⬜` 미구현.

| # | 상태 | 근거 |
| --- | --- | --- |
| 1, 2 | ✅ | `test_quant_engine`, `test_v374_pure_rules`(변동성 하한·타이브레이커) |
| 3 | 🟡 | 완료 일봉 사용은 기존 테스트, 200일 Upsert·소급 조정 감지 ⬜ |
| 4 | 🟡 | 재기준화(`/ca_confirm`)·호가 단위 ✅, 실시간 변환 파이프라인 ⬜ |
| 5, 7 | ✅ | `test_v374_pure_rules`(현금흐름 중립성·분배금 분리·epoch) |
| 6 | 🟡 | 대사 함수·BUY_BLOCK 플래그·`/classify` ✅, 대사 실행 ⬜ |
| 8, 9 | 🟡 | 백테스트 엔진·`select_final_slots` ✅, 실시간 15:00 파이프라인 ⬜ |
| 10 | ✅ | 지역 한도·보유 유동성 예외(`test_v374_pure_rules`, 엔진 테스트) |
| 11 | 🟡 | DB `CHECK`·미검증 격리 ✅(`test_v374_schema`), `/add` 거부는 기존 테스트 |
| 12, 13, 14 | ✅ | `NotAcceptedConfirmationTests`, 기존 `record_order_attempt` 재시도 테스트 |
| 15 | ✅ | `FillDeltaTests` |
| 16 | 🟡 | 전이표·`cancel_can_finalize`·지연 체결 보정 ✅, 취소 응답 유실 흐름 ⬜ |
| 17 | ✅ | 기존 멱등성 테스트 + partial unique index |
| 18 | 🟡 | 매도 선행 시한 규칙 문서화, 오케스트레이션 ⬜ |
| 19 | 🟡 | 스프레드·iNAV 검증 함수 기존, 지역별 한도·위험 축소 완화 ⬜ |
| 20 | ✅ | `test_v374_pure_rules`·`test_v375_rules`(보호성 손절 게이트·스탑 상수·원인별 허용/차단) |
| 21, 22 | 🟡 | 의무 저장·`SUPERSEDED`/`VOIDED` ✅, 15:14:30 일괄 취소·09:10 검문소 잡 ⬜ |
| 23 | 🟡 | 단계·히스테리시스 ✅, 자동 `EMERGENCY` 전이 배선 ⬜ |
| 24, 25 | ✅ | `OrderPolicyTruthTableTests`, `test_boot_recovery`, 기존 위험 모드 테스트 |
| 26 | 🟡 | `CALENDAR_MISMATCH` 플래그·자동 해제 ✅, 캘린더 이중 검증 잡 ⬜ |
| 27 | 🟡 | 스모크 세션 승인·소비 ✅, 15만 원 미만 생략·1주 단일 주문 게이트 연결 ⬜ |
| 28 | ✅ | `test_v374_schema` |
| 29 | 🟡 | WAL·단일 DB 연결 기존, `busy_timeout`·단일 writer 구현 ⬜ |
| 30 | 🟡 | v2→v3 이관 ✅, 롤백 실행 ⬜(스크립트 문법만) |
| 31 | 🟡 | 금지 시간 판정 ✅, 지연 큐 실행 ⬜ |
| 32, 38 | 🟡 | 파일 구조 검사(`test_host_operations`) ✅, 컨테이너·Runner 실제 격리 ⬜ |
| 33 | 🟡 | 토큰 서비스 기존 테스트 + 타이머 구조 ✅ |
| 34 | 🟡 | 3중 잠금 계산 ✅(`real_gate_inputs`), 게이트 호출부 연결 ⬜ |
| 35 | 🟡 | 네트워크 판단·Telegram 인증 ✅, 실제 재부팅 ⬜ |
| 36 | 🟡 | 어댑터 repr·기존 마스킹 테스트 ✅, 전 경로 검사 ⬜ |
| 37 | 🟡 | UPS 플래그 ✅, 안전 종료 절차 ⬜ |
| 39 | 🟡 | `rules.yaml` = 명세 10.3 ✅, `migrations` DDL 일치 일부, `docs/` 5종은 이번에 갱신(자동 비교 ⬜) |
| 40, 41, 62 | ✅ | `test_safety_flags`, `test_boot_recovery`, `test_v374_pure_rules` |
| 42 | 🟡 | 검증·재기준화 ✅, 감지 ⬜ |
| 43 | 🟡 | 동결 로직 ✅, 장중 대사 배선 ⬜ |
| 44 | ✅ | epoch·전액 출금(`test_v374_pure_rules`) |
| 45 | 🟡 | 크래시 복구 ✅, `post_close_reconcile` 확정 잡 ⬜ |
| 46 | ✅ | `NotAcceptedConfirmationTests` |
| 47 | ✅ | `test_kis_order_adapter` |
| 48 | 🟡 | 증분·병합·감소 거부 ✅, `ACCOUNT_MISMATCH` 플래그 연결 ⬜ |
| 49 | 🟡 | `release_emergency`·`rebase_hwm` ✅, `hwm_rebase_pending` 소비 ⬜ |
| 50 | 🟡 | 타이머 구조·상태 파일 ✅, 실제 systemd 실행 ⬜ |
| 51, 52 | ✅ | 슬롯 테스트·엔진 테스트 |
| 53, 54, 55, 56, 57, 58 | 🟡/⬜ | 53·56 부분(`order_policy`·DB), 54(마감 타임라인은 설정 검증만), 55 ⬜, 57 부분(호가표·`RF_INVALID` 프리뷰), 58 ⬜ |
| 59 | 🟡 | 승인 파일·DB 3중 잠금 ✅, 실전 TR 선택 연결 ⬜ |
| 60 | ✅ | `ConfirmationSecurityTests` |
| 61 | 🟡 | `epoch`·`V ≤ 0` 일부 ✅, 전 구간 가드 ⬜ |
| 63–75 | ⬜ | Phase 2(LLM) 미착수 |

## 4-1. v3.7.5 정렬 내역 (코드)

| 명세 변경 | 코드 반영 | 아직 연결 안 된 부분 |
| --- | --- | --- |
| D2 비상 재가격 삭제 | 재가격 함수·`replaces_order_id`·정정 인덱스·`execution.emergency` 삭제, `ux_orders_one_active_attempt` 1개 | — |
| V-B2 종결 트리거 | `trg_terminal_immutable`(체결 증가+잔량 0만), `LATE_FILL_CORRECTION` 잔량 0 고정 | — |
| V-B3 의무 수량 불변 | `trg_obligation_required_immutable`, `order_intents.obligation_id`, 체결 집계 동기화, 보유 0 → `VOIDED`, 활성 주문 있으면 대체 거부 | 09:10 검문소 잡이 의무 의도를 만드는 호출부 |
| D1 보호성 손절 | `PROTECTIVE_EXIT_REASONS`·`should_protective_stop`·`OrderKind/RiskAction.PROTECTIVE_EXIT_SELL`·게이트 `protective_exit`·미해결 재경보 | **스탑 감시 잡(위험 축소 매도 생성)과 재경보 잡 호출부**, 설정 `protective_exit.enabled` 를 `load_risk_flags` 에 전달하는 연결 |
| V-B5 대사 오탐 | 정지 상태 판정(`reconciliation_allowed`), `PROVISIONAL` 2단계, 조회 실패≠불일치, 허용 오차 1,000원, 고아 주문 처리 | 대사 실행 잡(체결 대금·잔고 조회 연결) |
| V-B6 회계 오염 | 오염 제거 단위가격, 늦은 분류 재계산 순수 함수(`restate_after_late_flow`), 악화 전이 비동결 | `daily_portfolio` 를 쓰는 마감 스냅샷 잡과 `/classify` 후 재계산 호출 |
| V-B7 파킹 결함 | 주간 프리뷰가 파킹 결함 시 `RF_INVALID` 프리뷰를 저장하고 교체 생략 | 15:00 Step B 가 `RF_INVALID` 를 읽어 교체를 건너뛰는 처리 |
| V-B8 현금 필드 | `accounting.valuation_cash_field` 설정·검사 | 평가액 교차 검증(`tot_evlu_amt`) 실행, PAPER 실측(runbook) |
| V-B9 내구성 | `synchronous=FULL`, 연결 시 WAL·FULL 검증 | 디렉터리 `fsync` 를 쓰는 호스트 스크립트(`refresh_kis_token.py` 등) 점검 |

마이그레이션 `004_spec_v375_schema.sql` 은 003 과 같은 테이블 재구성 방식으로 적용되며 `schema_migrations` 에 체크섬이 기록됩니다.
감사 보고서의 **Major 20건은 이번 정렬 범위 밖**입니다(`docs/audit_v3.7.4_review.md`).

## 4-2. Phase 1 마무리 1차 (Raspberry Pi 없이 가능한 배선)

| 항목 | 구현 | 파일 |
| --- | --- | --- |
| 위험 축소 매도 실행기(게이트→write-ahead→Bid1 1회→결과 기록, SAFE_HALT 보호성 손절 원인 판정) | ✅ | `src/order_executor.py` |
| `intraday_guard`(스탑 감시·15:10 이후 의무 기록만·조회 실패=`ACCOUNT_DATA_STALE`) | ✅ | `src/job_handlers.py` |
| `open_checkpoint`(의무 재매도·보유 0 → `VOIDED`) | ✅ | `src/job_handlers.py` |
| `order_cancel_sweep`(15:14:30 취소 요청 + 미체결 매도 의무화) | ✅ | `src/job_handlers.py` |
| `post_close_reconcile`(15:40: 정지 상태 대조·2회 재현 `ACCOUNT_MISMATCH`·예수금 대사·PeakPrice·일간 TWR/HWM 스냅샷) | ✅ | `src/job_handlers.py` |
| 안전 점검(UPS·시각 플래그 동기화, 24h 미해결 HALT 재경보) | ✅ (인트라데이 잡에 10분 주기로 합침) | `src/job_handlers.py`, `src/runtime.py` |
| KIS 응답 → 포트 변환(필드 누락 시 fail-closed) | ✅ (`prvs_rcdl_excc_amt`·`tot_evlu_amt` 는 `[UNVERIFIED]`) | `src/kis_broker.py` |
| 운영 조립(PAPER 전용, REAL 거부, 비밀은 환경변수만) | ✅ | `src/runtime.py`, `src/main.py` |
| PAPER 전용 주문 전송기 생성 경로(비공개 어댑터 경계 유지) | ✅ | `src/order_manager.py` |
| 스케줄 시각을 명세 9.1절에 정렬(`post_close_reconcile` 15:15:10 → 15:40, 가드 종료 15:25 → 15:30) | ✅ | `src/scheduler.py` |

버그 수정: 예수금 대사에서 첫 관측(`PROVISIONAL`) 직후 기준 예수금을 옮기면 다음 대사가 변동을 스스로 `DISMISSED` 시키는 문제를, 확정·해소 전에는 기준 예수금을 옮기지 않도록 처리했습니다.

## 4-3. Phase 1 마무리 2차 (Raspberry Pi 없이 가능한 나머지 잡)

| 항목 | 구현 | 파일 |
| --- | --- | --- |
| 주문·체결 동기화(체결 증분 기록·`positions` 반영·취소 확정·미접수 확정은 장 마감 후만·`PENDING_CONFIRMATION` 정지 원인 등록/해제) + 15초 `pending_confirmation_poller` | ✅ | `src/order_sync.py`, `src/job_handlers.py` |
| `morning_prep`: 200거래일 일봉 Upsert, 소급 조정(분배락) 감지 → 종목 격리 + `verified=0` 후보, ±30% 이상치 격리 | ✅ | `src/morning_prep.py` |
| `daily_pipeline`(15:00): 120MA·스탑·검증 상실 퇴출, 주간 첫 개장일 슬롯 확정·교체 매도, 매도 선행(시한 15:06)·매수(시한 15:10), 5.4절 수량식, 대표 파킹 배정, 슬롯·프리뷰 소비 | ✅ (제외 항목은 아래) | `src/daily_pipeline.py` |
| 전략 주문(교체·매수·파킹) 실행: 스프레드·iNAV 검증 후 매수 Ask1 / 매도 Bid1 | ✅ | `src/order_executor.py` |
| 18:05 일일 브리핑, 일요일 03:00 SQLite 백업+무결성 검사+8개 보관 | ✅ | `src/ops_jobs.py` |
| Telegram **알림 전송기**(마스킹·실패 무시) → 모든 잡의 CRITICAL/WARN 알림 연결 | ✅ | `src/ops_jobs.py`, `src/runtime.py` |

### 의도적으로 남겨 둔 것 (fail-closed 로 막혀 있음)

- **신규 매수는 실제로는 아직 나가지 않습니다:** `KisBroker.buyable_cash`(`nrcvb_buy_amt` 조회 TR 미확정 `[UNVERIFIED]`)가 예외를 올리고, iNAV 는 WebSocket 연동 전이라 `None` → 스프레드·iNAV 검증이 매수를 거부합니다. 매도(스탑·120MA·교체·의무 재주문)는 동작합니다.
- 리밸런싱 초과분(Drift) 매도, 파킹 현금화 매도, 슬롯 상관계수 검사(상관 함수 미주입), 5.2절 필터 전체의 15:00 재평가(현재는 120MA·검증·격리·데이터 상태만).
- `morning_prep` 의 XKRX 달력 ↔ KIS 휴장일 이중 검증(`exchange_calendars` 의존성 추가 필요 → 사용자 결정), 허용 일정 기반 분배락 자동 검증(`ALLOWLIST_SCHEDULE`)은 `/ca_confirm` 수동 경로만.
- `/approve_backtest` 는 산출물(커밋·전략 해시·기간) 연결 전이라 승인하지 않고 안내만 합니다. 2단계 확인 이력은 소비 표시가 없어 같은 명령을 다시 입력하면 같은 승인을 재사용할 수 있습니다(5분 만료 전 한정, 보강 대상).
- 아직 실행해 보지 않은 것: `main()` 의 Telegram 폴링(메인 스레드)+에이전트(작업 스레드) 구성은 실제 봇 토큰으로 검증하지 못했습니다(라즈베리파이·모의투자 단계에서 확인).

### 4-4. Telegram 명령 연결 (마무리 3차)

| 항목 | 구현 | 파일 |
| --- | --- | --- |
| 명령 19종 콜백(`/status`·`/preview`·`/pool`·`/trades`·`/health`·`/reconcile [adopt]`·`/pause`·`/stop`·`/resume`·`/training`·`/emergency [off]`·`/remove`·`/clear_halt`·`/classify`·`/ca_confirm`·`/approve_real`·`/approve_smoke`·`/approve_backtest`·`/add`) | ✅ | `src/telegram_actions.py` |
| 2단계 명령은 `pending_actions` 의 `APPROVED` 이력(명령 해시 일치)이 있어야만 실행 | ✅ | `src/telegram_actions.py` |
| 실패 답장은 고정 문구(예외 내용 비노출) + `COMMAND_FAILED` 기록 | ✅ | `src/telegram_actions.py` |
| `main()` 이 토큰·채팅 ID 가 있으면 폴링 시작(`allowed_user_ids` 주입 포함) | ✅(미실행 검증) | `src/main.py` |
| `hwm_rebase_pending` 소비 + 해제 후 `WARNING` 하한, 훈련 종료일 자동 해제, `/remove` 보유 종목 퇴출 | ✅ | `src/job_handlers.py`, `src/daily_pipeline.py` |
- 주간 교체 승계(misfire 후 다음 개장일 1회) 전용 로직: 현재는 "미소비 프리뷰가 있으면 다음 개장일에 같은 로직 재실행"으로 자연 승계됩니다(부분 실행되면 `CONSUMED`).
- 호스트(Raspberry Pi) 전용: systemd·워치독·CPU/온도 측정·UPS 안전 종료·실제 KIS 응답 필드 확정.

## 5. 권고 다음 순서

1. 이 보고서 3절의 **배선 항목**을 위험도순으로: `safe_halt`와 직접 관련된 것(주문 게이트 연결, `post_close_reconcile`, 기업행위 감지, 예수금 대사 실행) → `daily_pipeline` 오케스트레이션 → 장중 잡.
2. Raspberry Pi 5 에서 쉘·systemd·컨테이너 하드닝 실제 검증(`docs/operations_runbook.md` 5번).
3. `[UNVERIFIED]` 항목을 KIS 포털·KRX 규정으로 확인해 명세에서 제거.
4. Shadow Trading 2주 → 스모크 → REAL(11.3절 체크리스트).
